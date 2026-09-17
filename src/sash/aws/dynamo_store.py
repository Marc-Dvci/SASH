"""The store, over one DynamoDB table.

Every rule the SQLite store enforces with a UNIQUE constraint is enforced here
with a conditional write, which is the only mechanism that survives two Lambdas
racing on the same delivery:

* ``record_receipt`` writes ``RECEIPT#<request_id>`` with
  ``attribute_not_exists(pk)``. A retry loses the race and is reported as a
  duplicate, exactly as the SQLite ``IntegrityError`` path does.
* ``add_aq_sample`` writes ``AQ#<reported_at>`` the same way. The status endpoint
  returns the last reported reading, so polling faster than the device reports
  produces repeats, and a repeat is a health signal rather than a measurement.

Keys::

    HOUSEHOLD#<id>      DEVICE#<device_id>
    HOUSEHOLD#<id>      AQ#<reported_at:015.3f>#<device_id>
    HOUSEHOLD#<id>      VEHICLE#<t:015.3f>#<event_id>
    HOUSEHOLD#<id>      CONTACT#<t:015.3f>#<device_id>
    HOUSEHOLD#<id>      HEALTH#<t:015.3f>#<device_id>
    HOUSEHOLD#<id>      THRESHOLD#<t:015.3f>#<device_id>
    HOUSEHOLD#<id>      PLAN#<created_at:015.3f>#<plan_id>
    HOUSEHOLD#<id>      META#<key>
    RECEIPT#<request_id>  RECEIPT

Timestamps are zero-padded so lexicographic order is time order, which is what
makes a range query over a window one call.

``table`` is injected rather than constructed, so the tests drive this against an
in-memory double that implements the same conditional semantics.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Iterable

from ..store import Device

#: Receipts are only needed for as long as Ring might retry a delivery.
RECEIPT_TTL_S = 7 * 86400


class ConditionalCheckFailed(Exception):
    """Raised by a table double when a conditional write loses."""


def _t(value: float) -> str:
    return f"{float(value):015.3f}"


def _clean(value: Any) -> Any:
    """DynamoDB has no float type; numbers go in as Decimal."""
    if isinstance(value, float):
        return Decimal(str(round(value, 6)))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


class DynamoStore:
    """The subset of :class:`sash.store.Store` the ingest path needs."""

    def __init__(
        self,
        table: Any,
        household_id: str = "default",
        *,
        now: float = 0.0,
        read_ttl_s: float = 0.0,
    ):
        self.table = table
        self.pk = f"HOUSEHOLD#{household_id}"
        self.now = now
        #: How long a range read is reused inside one Lambda container. The
        #: product half reads the whole record to fit a household and every
        #: route asks again; the writers are other Lambdas on a five-minute
        #: schedule, so a minute of reuse costs nothing a household can see.
        #: Any write from this container clears it.
        self.read_ttl_s = read_ttl_s
        self._reads: dict[tuple[str, str | None, str | None], tuple[float, list]] = {}

    # --- generic ---------------------------------------------------------

    def _put(self, item: dict[str, Any], *, unique: bool = False) -> bool:
        self._reads.clear()
        kwargs: dict[str, Any] = {"Item": _clean(item)}
        if unique:
            kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
        try:
            self.table.put_item(**kwargs)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                return False
            raise

    def _query(self, prefix: str, *, start: str | None = None, end: str | None = None):
        if self.read_ttl_s > 0:
            key = (prefix, start, end)
            hit = self._reads.get(key)
            if hit is not None and time.monotonic() - hit[0] < self.read_ttl_s:
                return hit[1]
            rows = self._query_table(prefix, start=start, end=end)
            self._reads[key] = (time.monotonic(), rows)
            return rows
        return self._query_table(prefix, start=start, end=end)

    def _query_table(self, prefix: str, *, start: str | None = None, end: str | None = None):
        low = prefix + (start or "")
        high = prefix + (end or "￿")
        out: list[dict[str, Any]] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": "pk = :pk AND sk BETWEEN :lo AND :hi",
                "ExpressionAttributeValues": {":pk": self.pk, ":lo": low, ":hi": high},
            }
            if token:
                kwargs["ExclusiveStartKey"] = token
            page = self.table.query(**kwargs)
            out.extend(page.get("Items", []))
            token = page.get("LastEvaluatedKey")
            if not token:
                return out

    # --- meta ------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self._put({"pk": self.pk, "sk": f"META#{key}", "value": json.dumps(value)})

    def get_meta(self, key: str, default: Any = None) -> Any:
        item = self.table.get_item(Key={"pk": self.pk, "sk": f"META#{key}"}).get("Item")
        return json.loads(item["value"]) if item else default

    # --- devices ---------------------------------------------------------

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
        last_status_at: float | None = None,
        config_hash: str | None = None,
    ) -> None:
        existing = self.table.get_item(
            Key={"pk": self.pk, "sk": f"DEVICE#{device_id}"}
        ).get("Item") or {}
        # A household's own role mapping is never overwritten by a rediscovery:
        # Ring cannot say what a contact sensor is attached to, so that mapping
        # only exists here and losing it would silently empty the model.
        self._put(
            {
                "pk": self.pk,
                "sk": f"DEVICE#{device_id}",
                "device_id": device_id,
                "ring_name": ring_name,
                "family": family,
                "app_role": app_role or existing.get("app_role"),
                "label": label or existing.get("label"),
                "orientation": orientation or existing.get("orientation"),
                "online": online,
                "last_status_at": last_status_at,
                "config_hash": config_hash,
            }
        )

    def set_role(
        self,
        device_id: str,
        app_role: str,
        label: str | None = None,
        orientation: str | None = None,
    ) -> None:
        self._reads.clear()
        self.table.update_item(
            Key={"pk": self.pk, "sk": f"DEVICE#{device_id}"},
            # ``label`` and ``online`` are DynamoDB reserved words, so every
            # attribute name in an update goes through a placeholder.
            UpdateExpression="SET #r = :r, #l = :l, #o = :o",
            ExpressionAttributeNames={"#r": "app_role", "#l": "label", "#o": "orientation"},
            ExpressionAttributeValues={":r": app_role, ":l": label, ":o": orientation},
        )

    def devices(self, family: str | None = None, role: str | None = None) -> list[Device]:
        out = []
        for item in self._query("DEVICE#"):
            if family and item.get("family") != family:
                continue
            if role and item.get("app_role") != role:
                continue
            out.append(
                Device(
                    device_id=item["device_id"],
                    ring_name=item.get("ring_name", ""),
                    family=item.get("family", ""),
                    app_role=item.get("app_role"),
                    label=item.get("label"),
                    orientation=item.get("orientation"),
                    online=bool(item.get("online", True)),
                    last_status_at=_num(item.get("last_status_at")),
                    config_hash=item.get("config_hash"),
                )
            )
        return out

    def device(self, device_id: str) -> Device | None:
        for d in self.devices():
            if d.device_id == device_id:
                return d
        return None

    def set_online(self, device_id: str, online: bool, at: float) -> None:
        self._put(
            {
                "pk": self.pk,
                "sk": f"HEALTH#{_t(at)}#{device_id}",
                "device_id": device_id,
                "online": online,
                "at": at,
            }
        )
        self._reads.clear()
        self.table.update_item(
            Key={"pk": self.pk, "sk": f"DEVICE#{device_id}"},
            UpdateExpression="SET #on = :o",
            ExpressionAttributeNames={"#on": "online"},
            ExpressionAttributeValues={":o": online},
        )

    def health_transitions(self, device_id: str) -> list[tuple[float, bool]]:
        return [
            (float(i["at"]), bool(i["online"]))
            for i in self._query("HEALTH#")
            if i.get("device_id") == device_id
        ]

    # --- idempotency -----------------------------------------------------

    def seen_request(self, request_id: str) -> bool:
        key = {"pk": f"RECEIPT#{request_id}", "sk": "RECEIPT"}
        return bool(self.table.get_item(Key=key).get("Item"))

    def record_receipt(
        self, request_id: str, event_type: str, device_id: str | None
    ) -> bool:
        """True if this is the first time. False for a retry Ring already sent.

        The conditional write is the whole mechanism. Read-then-write would let
        two concurrent Lambdas both decide they were first.
        """
        return self._put(
            {
                "pk": f"RECEIPT#{request_id}",
                "sk": "RECEIPT",
                "event_type": event_type,
                "device_id": device_id,
                "expires_at": int(self.now + RECEIPT_TTL_S) if self.now else None,
            },
            unique=True,
        )

    def receipt_count(self) -> int:  # pragma: no cover - a scan, for the health page only
        return int(self.table.scan(Select="COUNT").get("Count", 0))

    # --- events ----------------------------------------------------------

    def add_vehicle_event(
        self, event_id: str, camera_id: str, event_time: float, source: str
    ) -> None:
        self._put(
            {
                "pk": self.pk,
                "sk": f"VEHICLE#{_t(event_time)}#{event_id}",
                "camera_id": camera_id,
                "event_time": event_time,
                "source": source,
            }
        )

    def vehicle_event_times(
        self, start: float | None = None, end: float | None = None
    ) -> list[float]:
        items = self._query(
            "VEHICLE#",
            start=None if start is None else _t(start),
            end=None if end is None else _t(end),
        )
        return sorted(float(i["event_time"]) for i in items)

    def vehicle_event_count(self) -> int:
        return len(self._query("VEHICLE#"))

    def add_contact_event(
        self, event_id: str, device_id: str, event_time: float, state: str
    ) -> None:
        self._put(
            {
                "pk": self.pk,
                "sk": f"CONTACT#{_t(event_time)}#{device_id}",
                "event_id": event_id,
                "device_id": device_id,
                "event_time": event_time,
                "state": state,
            }
        )

    def contact_events(self, device_id: str | None = None) -> list[tuple[str, float, str]]:
        rows = [
            (i["device_id"], float(i["event_time"]), i["state"])
            for i in self._query("CONTACT#")
            if device_id is None or i.get("device_id") == device_id
        ]
        rows.sort(key=lambda r: r[1])
        return rows

    def add_threshold_event(
        self,
        event_id: str,
        device_id: str,
        event_time: float,
        event_type: str,
        level: float | None,
    ) -> None:
        self._put(
            {
                "pk": self.pk,
                "sk": f"THRESHOLD#{_t(event_time)}#{device_id}",
                "event_id": event_id,
                "device_id": device_id,
                "event_time": event_time,
                "event_type": event_type,
                "level": level,
            }
        )

    def threshold_events(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._query("THRESHOLD#")]

    # --- air quality -----------------------------------------------------

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
        """False when this ``reported_at`` was already stored.

        `GET /v1/devices/{id}/status` returns the last reported reading, not a
        live one, so this refusal is the common case in production rather than
        an edge case.
        """
        return self._put(
            {
                "pk": self.pk,
                "sk": f"AQ#{_t(reported_at)}#{device_id}",
                "device_id": device_id,
                "reported_at": reported_at,
                "fetched_at": fetched_at,
                "pm25": pm25,
                "co": co,
                "temperature": temperature,
                "humidity": humidity,
            },
            unique=True,
        )

    def aq_samples(
        self,
        device_id: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[dict[str, Any]]:
        items = self._query(
            "AQ#",
            start=None if start is None else _t(start),
            end=None if end is None else _t(end),
        )
        rows = [
            {
                "device_id": i["device_id"],
                "reported_at": float(i["reported_at"]),
                "fetched_at": float(i["fetched_at"]),
                "pm25": _num(i.get("pm25")),
                "co": _num(i.get("co")),
                "temperature": _num(i.get("temperature")),
                "humidity": _num(i.get("humidity")),
            }
            for i in items
            if device_id is None or i.get("device_id") == device_id
        ]
        rows.sort(key=lambda r: r["reported_at"])
        return rows

    def latest_aq_sample(self, device_id: str | None = None) -> dict[str, Any] | None:
        rows = self.aq_samples(device_id)
        return rows[-1] if rows else None

    # --- plans -----------------------------------------------------------

    def add_plan(self, plan: dict[str, Any]) -> None:
        self._put(
            {
                "pk": self.pk,
                "sk": f"PLAN#{_t(plan['created_at'])}#{plan['plan_id']}",
                **plan,
            }
        )

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        for item in self._query("PLAN#"):
            if item.get("plan_id") != plan_id:
                continue
            merged = dict(item)
            merged.update(fields)
            self._put(merged)
            return

    def plans(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            {k: (_num(v) if isinstance(v, Decimal) else v) for k, v in i.items()}
            for i in self._query("PLAN#")
            if status is None or i.get("status") == status
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows


def _is_conditional_failure(exc: Exception) -> bool:
    if isinstance(exc, ConditionalCheckFailed):
        return True
    name = type(exc).__name__
    if name == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        return code in ("ConditionalCheckFailedException", "TransactionCanceledException")
    return False


def table_from_env() -> Any:  # pragma: no cover - needs boto3 and a real table
    import os

    import boto3

    return boto3.resource("dynamodb").Table(os.environ["SASH_TABLE"])


def _unused(_: Iterable[Any]) -> None:  # pragma: no cover
    pass
