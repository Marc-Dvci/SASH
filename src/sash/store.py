"""SQLite persistence for SASH.

Ring exposes no historical time series for sensors: webhook delivery is the only
source of sensor event data, and the status endpoint returns the last reported
reading rather than a stream. Everything SASH knows about a home is therefore
accumulated here, from the moment the account is linked.

All timestamps are epoch seconds, UTC.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    ring_name       TEXT NOT NULL,
    family          TEXT NOT NULL,     -- camera | contact_sensor | air_quality_monitor
    app_role        TEXT,              -- street_camera | opening | indoor_air | unused
    label           TEXT,              -- 'Street window', 'Courtyard window', ...
    orientation     TEXT,              -- street | courtyard | garden | unknown
    online          INTEGER DEFAULT 1,
    last_status_at  REAL,
    config_hash     TEXT,              -- motion-zone configuration epoch
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_receipts (
    request_id   TEXT PRIMARY KEY,
    received_at  REAL NOT NULL,
    event_type   TEXT,
    device_id    TEXT
);

CREATE TABLE IF NOT EXISTS vehicle_events (
    event_id    TEXT PRIMARY KEY,
    camera_id   TEXT NOT NULL,
    event_time  REAL NOT NULL,
    source      TEXT NOT NULL          -- webhook | history
);
CREATE INDEX IF NOT EXISTS ix_vehicle_time ON vehicle_events(event_time);

CREATE TABLE IF NOT EXISTS contact_events (
    event_id    TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    event_time  REAL NOT NULL,
    state       TEXT NOT NULL          -- open | closed
);
CREATE INDEX IF NOT EXISTS ix_contact_time ON contact_events(device_id, event_time);

CREATE TABLE IF NOT EXISTS aq_samples (
    device_id    TEXT NOT NULL,
    reported_at  REAL NOT NULL,
    fetched_at   REAL NOT NULL,
    pm25         REAL,
    co           REAL,
    temperature  REAL,
    humidity     REAL,
    PRIMARY KEY (device_id, reported_at)
);
CREATE INDEX IF NOT EXISTS ix_aq_reported ON aq_samples(reported_at);

CREATE TABLE IF NOT EXISTS aq_threshold_events (
    event_id    TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    event_time  REAL NOT NULL,
    event_type  TEXT NOT NULL,
    level       REAL
);

CREATE TABLE IF NOT EXISTS device_health (
    device_id  TEXT NOT NULL,
    at         REAL NOT NULL,
    online     INTEGER NOT NULL,
    PRIMARY KEY (device_id, at)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id             TEXT PRIMARY KEY,
    created_at          REAL NOT NULL,
    opening_id          TEXT NOT NULL,
    opening_label       TEXT,
    start_at            REAL NOT NULL,
    duration_min        REAL NOT NULL,
    target_exchanges    REAL,
    pred_exchanges      REAL,
    pred_intake         REAL,
    pred_intake_lo      REAL,
    pred_intake_hi      REAL,
    baseline_intake     REAL,
    status              TEXT NOT NULL,  -- offered | executed | verified | abandoned
    realized_exchanges  REAL,
    realized_intake     REAL,
    verdict             TEXT,           -- inside_interval | above | below | unverifiable
    detail              TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


@dataclass(frozen=True)
class Device:
    device_id: str
    ring_name: str
    family: str
    app_role: str | None
    label: str | None
    orientation: str | None
    online: bool
    last_status_at: float | None
    config_hash: str | None


class Store:
    """Thread-safe SQLite wrapper. One database file per household."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- generic ----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)))

    def reset(self) -> None:
        tables = (
            "devices",
            "webhook_receipts",
            "vehicle_events",
            "contact_events",
            "aq_samples",
            "aq_threshold_events",
            "device_health",
            "plans",
            "meta",
        )
        with self._lock:
            for table in tables:
                self._conn.execute("DELETE FROM " + table)
            self._conn.commit()

    # --- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    # --- devices ----------------------------------------------------------
    def upsert_device(
        self,
        device_id: str,
        ring_name: str,
        family: str,
        *,
        app_role: str | None = None,
        label: str | None = None,
        orientation: str | None = None,
        online: bool = True,
        config_hash: str | None = None,
    ) -> None:
        now = time.time()
        self._exec(
            """
            INSERT INTO devices(device_id, ring_name, family, app_role, label,
                                orientation, online, last_status_at, config_hash, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                ring_name = excluded.ring_name,
                family    = excluded.family,
                app_role  = COALESCE(excluded.app_role, devices.app_role),
                label     = COALESCE(excluded.label, devices.label),
                orientation = COALESCE(excluded.orientation, devices.orientation),
                online    = excluded.online,
                last_status_at = excluded.last_status_at,
                config_hash = COALESCE(excluded.config_hash, devices.config_hash)
            """,
            (
                device_id,
                ring_name,
                family,
                app_role,
                label,
                orientation,
                int(online),
                now,
                config_hash,
                now,
            ),
        )

    def set_role(
        self,
        device_id: str,
        app_role: str,
        label: str | None = None,
        orientation: str | None = None,
    ) -> None:
        self._exec(
            "UPDATE devices SET app_role=?, label=COALESCE(?,label), "
            "orientation=COALESCE(?,orientation) WHERE device_id=?",
            (app_role, label, orientation, device_id),
        )

    def devices(self, family: str | None = None, role: str | None = None) -> list[Device]:
        sql = "SELECT * FROM devices"
        args: list[Any] = []
        clauses = []
        if family:
            clauses.append("family=?")
            args.append(family)
        if role:
            clauses.append("app_role=?")
            args.append(role)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY family, ring_name"
        return [
            Device(
                device_id=r["device_id"],
                ring_name=r["ring_name"],
                family=r["family"],
                app_role=r["app_role"],
                label=r["label"],
                orientation=r["orientation"],
                online=bool(r["online"]),
                last_status_at=r["last_status_at"],
                config_hash=r["config_hash"],
            )
            for r in self._query(sql, args)
        ]

    def device(self, device_id: str) -> Device | None:
        found = [d for d in self.devices() if d.device_id == device_id]
        return found[0] if found else None

    def set_online(self, device_id: str, online: bool, at: float) -> None:
        self._exec(
            "UPDATE devices SET online=?, last_status_at=? WHERE device_id=?",
            (int(online), at, device_id),
        )
        self._exec(
            "INSERT OR REPLACE INTO device_health(device_id, at, online) VALUES(?,?,?)",
            (device_id, at, int(online)),
        )

    def health_transitions(self, device_id: str) -> list[tuple[float, bool]]:
        return [
            (r["at"], bool(r["online"]))
            for r in self._query(
                "SELECT at, online FROM device_health WHERE device_id=? ORDER BY at",
                (device_id,),
            )
        ]

    # --- webhook idempotency ---------------------------------------------
    def seen_request(self, request_id: str) -> bool:
        return bool(
            self._query("SELECT 1 FROM webhook_receipts WHERE request_id=?", (request_id,))
        )

    def record_receipt(self, request_id: str, event_type: str, device_id: str | None) -> bool:
        """Return True if this is the first time we have seen ``request_id``."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO webhook_receipts(request_id, received_at, event_type, device_id) "
                    "VALUES(?,?,?,?)",
                    (request_id, time.time(), event_type, device_id),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def receipt_count(self) -> int:
        return self._query("SELECT COUNT(*) c FROM webhook_receipts")[0]["c"]

    # --- vehicle-motion activity -----------------------------------------
    def add_vehicle_event(
        self, event_id: str, camera_id: str, event_time: float, source: str
    ) -> None:
        self._exec(
            "INSERT OR IGNORE INTO vehicle_events(event_id, camera_id, event_time, source) "
            "VALUES(?,?,?,?)",
            (event_id, camera_id, event_time, source),
        )

    def vehicle_event_times(
        self, start: float | None = None, end: float | None = None
    ) -> list[float]:
        sql = "SELECT event_time FROM vehicle_events"
        args: list[Any] = []
        if start is not None and end is not None:
            sql += " WHERE event_time >= ? AND event_time < ?"
            args = [start, end]
        sql += " ORDER BY event_time"
        return [r["event_time"] for r in self._query(sql, args)]

    def vehicle_event_count(self) -> int:
        return self._query("SELECT COUNT(*) c FROM vehicle_events")[0]["c"]

    # --- opening state ----------------------------------------------------
    def add_contact_event(
        self, event_id: str, device_id: str, event_time: float, state: str
    ) -> None:
        self._exec(
            "INSERT OR IGNORE INTO contact_events(event_id, device_id, event_time, state) "
            "VALUES(?,?,?,?)",
            (event_id, device_id, event_time, state),
        )

    def contact_events(self, device_id: str | None = None) -> list[tuple[str, float, str]]:
        sql = "SELECT device_id, event_time, state FROM contact_events"
        args: list[Any] = []
        if device_id:
            sql += " WHERE device_id=?"
            args.append(device_id)
        sql += " ORDER BY event_time"
        return [(r["device_id"], r["event_time"], r["state"]) for r in self._query(sql, args)]

    # --- air quality ------------------------------------------------------
    def add_aq_sample(
        self,
        device_id: str,
        reported_at: float,
        fetched_at: float,
        pm25: float | None,
        co: float | None,
        temperature: float | None,
        humidity: float | None,
    ) -> bool:
        """Insert a sample. Returns False when ``reported_at`` was already stored.

        The status endpoint returns the last reported reading, so polling it
        faster than the device reports yields repeats. A repeat is a health
        signal, never a new measurement.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO aq_samples(device_id, reported_at, fetched_at, pm25, co, "
                    "temperature, humidity) VALUES(?,?,?,?,?,?,?)",
                    (device_id, reported_at, fetched_at, pm25, co, temperature, humidity),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def aq_samples(
        self,
        device_id: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM aq_samples"
        clauses: list[str] = []
        args: list[Any] = []
        if device_id:
            clauses.append("device_id=?")
            args.append(device_id)
        if start is not None:
            clauses.append("reported_at >= ?")
            args.append(start)
        if end is not None:
            clauses.append("reported_at < ?")
            args.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY reported_at"
        return [dict(r) for r in self._query(sql, args)]

    def latest_aq_sample(self, device_id: str | None = None) -> dict[str, Any] | None:
        rows = self.aq_samples(device_id)
        return rows[-1] if rows else None

    def add_threshold_event(
        self,
        event_id: str,
        device_id: str,
        event_time: float,
        event_type: str,
        level: float | None,
    ) -> None:
        self._exec(
            "INSERT OR IGNORE INTO aq_threshold_events(event_id, device_id, event_time, "
            "event_type, level) VALUES(?,?,?,?,?)",
            (event_id, device_id, event_time, event_type, level),
        )

    def threshold_events(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._query("SELECT * FROM aq_threshold_events ORDER BY event_time")
        ]

    # --- ventilation plans (the verification ledger) ----------------------
    def add_plan(self, plan: dict[str, Any]) -> None:
        cols = ",".join(plan.keys())
        marks = ",".join("?" for _ in plan)
        self._exec(
            "INSERT OR REPLACE INTO plans(" + cols + ") VALUES(" + marks + ")",
            list(plan.values()),
        )

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(k + "=?" for k in fields)
        self._exec("UPDATE plans SET " + sets + " WHERE plan_id=?", [*fields.values(), plan_id])

    def plans(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM plans"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY start_at DESC"
        return [dict(r) for r in self._query(sql, args)]
