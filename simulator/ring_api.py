"""Ring Partner API payloads, and a local server that speaks them.

A physical Ring device is not required for the Ring track, and the Ring
Playground simulates camera events but -- as of the August 2026 release notes --
does not document simulation for the sensor APIs that entered Early Access on
24 August 2026. This module fills that gap locally: it serves the documented
endpoints and pushes documented, HMAC-signed webhooks, driven by the physical
home in ``simulator/home.py``.

The point is not that the simulator is realistic; it is that **SASH cannot tell
the difference**. The only thing that changes between this and the real Ring
service is ``RING_API_BASE_URL``. The same client, the same signature check, the
same idempotency, the same handling of ``reported_at``.

Media endpoints are served here deliberately, and they answer. SASH still never
calls them: the refusal is on the client side, where it belongs, and
``tests/test_no_media.py`` proves it against a server that would have complied.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .home import SimResult

# --- payload builders -------------------------------------------------------


def _envelope(event_type: str, device_id: str, attributes: dict[str, Any], account_id: str) -> dict:
    return {
        "meta": {
            "request_id": uuid.uuid4().hex,
            "account_id": account_id,
            "version": "2026-08-24",
        },
        "data": {
            "type": event_type,
            "id": uuid.uuid4().hex,
            "attributes": {"source": device_id, **attributes},
        },
    }


def motion_payload(
    device_id: str, timestamp: float, sub_type: str, account_id: str = "sim-account"
) -> dict:
    """``motion_detected`` with ``sub_type`` in motion|human|vehicle|other_motion."""
    return _envelope(
        "motion_detected",
        device_id,
        {"timestamp": int(timestamp * 1000), "sub_type": sub_type},
        account_id,
    )


def contact_payload(
    device_id: str, timestamp: float, opened: bool, account_id: str = "sim-account"
) -> dict:
    """``contact_sensor_faulted`` when the opening opens, ``_cleared`` when it shuts."""
    return _envelope(
        "contact_sensor_faulted" if opened else "contact_sensor_cleared",
        device_id,
        {"timestamp": int(timestamp * 1000), "contact_detection": {"faulted": opened}},
        account_id,
    )


def threshold_payload(
    device_id: str,
    timestamp: float,
    event_type: str,
    level: float | None = None,
    account_id: str = "sim-account",
) -> dict:
    attrs: dict[str, Any] = {"timestamp": int(timestamp * 1000)}
    if level is not None:
        attrs["pm25_level" if event_type.startswith("pm25") else "co_level"] = level
    return _envelope(event_type, device_id, attrs, account_id)


def lifecycle_payload(
    device_id: str, timestamp: float, event_type: str, account_id: str = "sim-account"
) -> dict:
    return _envelope(event_type, device_id, {"timestamp": int(timestamp * 1000)}, account_id)


def aqm_status(
    device_id: str, reported_at: float, reading: dict[str, Any], online: bool = True
) -> dict:
    """The shape ``GET /v1/devices/{id}/status`` returns for an Air Quality Monitor.

    ``reported_at`` is the timestamp of the reading, not of the response. The
    endpoint returns the last reported reading; the two are rarely the same.
    """
    return {
        "data": {
            "id": device_id,
            "type": "device_status",
            "reported_at": int(reported_at * 1000),
            "attributes": {
                "online": online,
                "temperature": reading["temperature"],
                "humidity": reading["humidity"],
                "pm2_5": reading["pm2_5"],
                "co": reading["co"],
            },
        }
    }


def contact_status(device_id: str, faulted: bool, reported_at: float) -> dict:
    return {
        "data": {
            "id": device_id,
            "type": "device_status",
            "reported_at": int(reported_at * 1000),
            "attributes": {"online": True, "contact_detection": {"faulted": faulted}},
        }
    }


def camera_status(device_id: str, online: bool, reported_at: float) -> dict:
    return {
        "data": {
            "id": device_id,
            "type": "device_status",
            "reported_at": int(reported_at * 1000),
            "attributes": {"online": online, "motion_detection": {"enabled": True}},
        }
    }


# --- the simulated household ------------------------------------------------

CAMERA_ID = "cam-front-1a2b"
AQM_ID = "aqm-living-9f3c"
ACCOUNT_ID = "sim-account-7c41"


@dataclass
class SimHome:
    """A Ring account with one camera, one Air Quality Monitor and N contacts."""

    result: SimResult
    labels: dict[str, str]
    params: Any = None
    orientations: dict[str, str] = field(default_factory=dict)
    camera_id: str = CAMERA_ID
    aqm_id: str = AQM_ID
    account_id: str = ACCOUNT_ID
    clock: float = 0.0

    def __post_init__(self) -> None:
        if not self.clock:
            self.clock = self.result.t0

    # --- device catalogue -------------------------------------------------
    def devices(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "id": self.camera_id,
                "device_type": "camera",
                "attributes": {
                    "name": "Front Door",
                    "online": self.camera_online(self.clock),
                    "capabilities": {
                        "motion_detection": True,
                        "video": True,
                        "live_view": True,
                    },
                    "configurations": {
                        "motion_zones": [
                            {"id": "zone-1", "name": "Pavement and road", "enabled": True}
                        ],
                        "privacy_zones": [],
                    },
                    "location": {"id": "loc-1", "name": "Home"},
                },
            },
            {
                "id": self.aqm_id,
                "device_type": "air_quality_monitor",
                "attributes": {
                    "name": "Living Room Air",
                    "online": True,
                    "capabilities": {"pm2_5": True, "co": True, "temperature": True,
                                     "humidity": True},
                    "location": {"id": "loc-1", "name": "Home"},
                },
            },
        ]
        for opening_id, label in self.labels.items():
            out.append(
                {
                    "id": opening_id,
                    "device_type": "contact_sensor",
                    "attributes": {
                        "name": label,
                        "online": True,
                        "capabilities": {"contact_detection": True, "tamper": True},
                        "location": {"id": "loc-1", "name": "Home"},
                    },
                }
            )
        return out

    # --- state at the current clock --------------------------------------
    def camera_online(self, t: float) -> bool:
        return not any(a <= t < b for a, b in self.result.camera_offline)

    def opening_open(self, opening_id: str, t: float) -> bool:
        return any(a <= t < b for a, b in self.result.open_intervals.get(opening_id, []))

    def last_report(self, t: float) -> dict[str, Any] | None:
        chosen = None
        for row in self.result.aqm_reports:
            if row["reported_at"] <= t:
                chosen = row
            else:
                break
        return chosen

    def status(self, device_id: str) -> dict[str, Any]:
        t = self.clock
        if device_id == self.aqm_id:
            row = self.last_report(t)
            if row is None:
                return aqm_status(
                    device_id,
                    t,
                    {"temperature": 20.0, "humidity": 45.0, "pm2_5": 5.0, "co": 0.1},
                )
            return aqm_status(device_id, row["reported_at"], row)
        if device_id == self.camera_id:
            return camera_status(device_id, self.camera_online(t), t)
        if device_id in self.labels:
            return contact_status(device_id, self.opening_open(device_id, t), t)
        raise KeyError(device_id)

    def vehicle_history(self, start: float, end: float, limit: int = 200) -> list[dict[str, Any]]:
        rows = []
        for stamp in self.result.vehicle_event_times:
            if start <= stamp < end:
                rows.append(
                    {
                        "id": f"hist-{int(stamp * 1000)}",
                        "type": "event",
                        "attributes": {
                            "event_type": "motion.vehicle",
                            "start_time": int(stamp * 1000),
                            "end_time": int((stamp + 12) * 1000),
                            "device_id": self.camera_id,
                        },
                    }
                )
                if len(rows) >= limit:
                    break
        return rows

    # --- the event tape ---------------------------------------------------
    def events_between(self, start: float, end: float) -> list[dict[str, Any]]:
        """Every webhook Ring would have delivered in ``(start, end]``, in order."""
        out: list[tuple[float, dict[str, Any]]] = []

        for stamp in self.result.vehicle_event_times:
            if start < stamp <= end:
                out.append((stamp, motion_payload(self.camera_id, stamp, "vehicle", self.account_id)))

        # Human motion is delivered too. SASH drops it at the door; a demo that
        # never sends it would be hiding the part that matters.
        for stamp in self.result.vehicle_event_times[::7]:
            if start < stamp + 3 <= end:
                out.append(
                    (stamp + 3, motion_payload(self.camera_id, stamp + 3, "human", self.account_id))
                )

        for opening_id, intervals in self.result.open_intervals.items():
            for a, b in intervals:
                if start < a <= end:
                    out.append((a, contact_payload(opening_id, a, True, self.account_id)))
                if start < b <= end:
                    out.append((b, contact_payload(opening_id, b, False, self.account_id)))

        exceeded = False
        for row in self.result.aqm_reports:
            stamp = row["reported_at"]
            if row["pm2_5"] > 35.0 and not exceeded:
                exceeded = True
                if start < stamp <= end:
                    out.append(
                        (stamp, threshold_payload(self.aqm_id, stamp, "pm25_exceeded", row["pm2_5"], self.account_id))
                    )
            elif row["pm2_5"] <= 30.0 and exceeded:
                exceeded = False
                if start < stamp <= end:
                    out.append(
                        (stamp, threshold_payload(self.aqm_id, stamp, "pm25_cleared", row["pm2_5"], self.account_id))
                    )

        for a, b in self.result.camera_offline:
            if start < a <= end:
                out.append((a, lifecycle_payload(self.camera_id, a, "device_offline", self.account_id)))
            if start < b <= end:
                out.append((b, lifecycle_payload(self.camera_id, b, "device_online", self.account_id)))

        out.sort(key=lambda item: item[0])
        return [payload for _, payload in out]

    def reports_between(self, start: float, end: float) -> list[dict[str, Any]]:
        return [r for r in self.result.aqm_reports if start < r["reported_at"] <= end]


# --- the HTTP server --------------------------------------------------------


def create_app(home: SimHome, hmac_key: str, webhook_url: str | None = None):
    """A FastAPI app serving the documented Ring endpoints for ``home``."""
    import hashlib
    import hmac as hmac_mod

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Ring Partner API (local simulator)", version="2026-08-24")
    app.state.home = home
    app.state.webhook_url = webhook_url
    app.state.delivered = 0

    def _sign(body: bytes) -> str:
        return "sha256=" + hmac_mod.new(hmac_key.encode(), body, hashlib.sha256).hexdigest()

    @app.post("/oauth/token")
    async def token() -> dict:
        return {
            "access_token": "sim-" + uuid.uuid4().hex,
            "refresh_token": "sim-refresh-" + uuid.uuid4().hex,
            "expires_in": 14400,
            "token_type": "Bearer",
            "scope": "devices.read sensors.read events.read",
            "account_id": home.account_id,
        }

    @app.get("/v1/users/me")
    async def me() -> dict:
        return {"data": {"id": home.account_id, "type": "user"}}

    @app.get("/v1/devices")
    async def devices(include: str = "") -> dict:
        return {"data": home.devices()}

    @app.get("/v1/devices/{device_id}")
    async def device(device_id: str) -> Any:
        for entry in home.devices():
            if entry["id"] == device_id:
                return {"data": entry}
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/status")
    async def status(device_id: str) -> Any:
        try:
            return home.status(device_id)
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/capabilities")
    async def capabilities(device_id: str) -> Any:
        for entry in home.devices():
            if entry["id"] == device_id:
                return {"data": entry["attributes"].get("capabilities", {})}
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/configurations")
    async def configurations(device_id: str) -> Any:
        for entry in home.devices():
            if entry["id"] == device_id:
                return {"data": entry["attributes"].get("configurations", {})}
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/location")
    async def location(device_id: str) -> Any:
        return {"data": {"id": "loc-1", "name": "Home"}}

    @app.get("/v1/history/devices/{device_id}/events")
    async def history(
        device_id: str,
        event_types: str = "motion.vehicle",
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 200,
    ) -> dict:
        start = (start_time / 1000.0) if start_time else home.result.t0
        end = (end_time / 1000.0) if end_time else home.clock
        return {"data": home.vehicle_history(start, end, limit)}

    # Media endpoints answer, so the client-side refusal is what is being tested.
    @app.post("/v1/devices/{device_id}/media/video/download")
    async def video(device_id: str) -> dict:
        return {"data": {"url": "https://example.invalid/clip.mp4"}}

    @app.post("/v1/devices/{device_id}/media/image/download")
    async def image(device_id: str) -> dict:
        return {"data": {"url": "https://example.invalid/snap.jpg"}}

    @app.post("/v1/devices/{device_id}/media/streaming/whep/sessions")
    async def whep(device_id: str) -> dict:
        return {"data": {"session_id": uuid.uuid4().hex}}

    # --- simulator control ------------------------------------------------
    @app.get("/sim/state")
    async def sim_state() -> dict:
        return {
            "clock": home.clock,
            "t0": home.result.t0,
            "end": home.result.t0 + home.result.minutes * 60.0,
            "delivered": app.state.delivered,
            "camera_online": home.camera_online(home.clock),
            "openings": {
                oid: home.opening_open(oid, home.clock) for oid in home.labels
            },
        }

    @app.post("/sim/advance")
    async def advance(request: Request) -> dict:
        body = await request.json() if await request.body() else {}
        minutes = float(body.get("minutes", 15))
        start, end = home.clock, home.clock + minutes * 60.0
        home.clock = end
        payloads = home.events_between(start, end)
        if app.state.webhook_url:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for payload in payloads:
                    raw = json.dumps(payload).encode()
                    await client.post(
                        app.state.webhook_url,
                        content=raw,
                        headers={
                            "X-Signature": _sign(raw),
                            "Content-Type": "application/json",
                        },
                    )
                    app.state.delivered += 1
        return {"clock": home.clock, "delivered": len(payloads)}

    @app.post("/sim/vehicle")
    async def vehicle(request: Request) -> dict:
        """Fire a vehicle-motion event now, the way the Playground does."""
        stamp = home.clock
        payload = motion_payload(home.camera_id, stamp, "vehicle", home.account_id)
        if app.state.webhook_url:
            raw = json.dumps(payload).encode()
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    app.state.webhook_url,
                    content=raw,
                    headers={"X-Signature": _sign(raw), "Content-Type": "application/json"},
                )
                app.state.delivered += 1
        return {"sent": payload["data"]["type"], "at": stamp}

    return app
