"""Ring Partner API payloads, and a local server that speaks them.

A physical Ring device is not required for the Ring track. The Ring Playground
gives a thirty-minute token for one simulated doorbell: device discovery,
status, Event History and live view answer, but it delivers no webhooks and has
no sensors, which entered Early Access on 24 August 2026. This module fills
that gap locally: it serves the documented endpoints and pushes documented,
HMAC-signed webhooks, driven by the physical home in ``simulator/home.py``.

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
from datetime import datetime, timezone
from typing import Any

from .home import SimResult

# --- payload builders -------------------------------------------------------


def iso(stamp: float) -> str:
    """An ISO 8601 UTC timestamp with milliseconds, the way Ring writes one."""
    whole = int(stamp // 1)
    millis = int(round((stamp - whole) * 1000))
    if millis == 1000:
        whole, millis = whole + 1, 0
    return datetime.fromtimestamp(whole, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def _envelope(event_type: str, device_id: str, attributes: dict[str, Any], account_id: str) -> dict:
    """The documented webhook envelope, payload version 1.1."""
    stamp_ms = int(attributes.get("timestamp", 0))
    return {
        "meta": {
            "version": "1.1",
            "time": iso(stamp_ms / 1000.0 + 0.4),
            "request_id": f"{uuid.uuid4()}-{stamp_ms}-{event_type[:13]}",
            "account_id": account_id,
        },
        "data": {
            "id": f"{device_id}_{event_type}_{stamp_ms}",
            "type": event_type,
            "attributes": {"source": device_id, "source_type": "devices", **attributes},
            "relationships": {"devices": {"links": {"self": f"/v1/devices/{device_id}"}}},
        },
    }


def motion_payload(
    device_id: str, timestamp: float, sub_type: str, account_id: str = "sim-account"
) -> dict:
    """``motion_detected`` with ``sub_type`` in motion|human|vehicle|other_motion."""
    stamp_ms = int(timestamp * 1000)
    readable = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return _envelope(
        "motion_detected",
        device_id,
        {
            "timestamp": stamp_ms,
            "timestamp_readable": readable,
            "sub_type": sub_type,
            "component_ids": ["0"],
        },
        account_id,
    )


def contact_payload(
    device_id: str, timestamp: float, opened: bool, account_id: str = "sim-account"
) -> dict:
    """``contact_sensor_faulted`` when the opening opens, ``_cleared`` when it shuts."""
    return _envelope(
        "contact_sensor_faulted" if opened else "contact_sensor_cleared",
        device_id,
        {"timestamp": int(timestamp * 1000)},
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
    if level is not None and event_type.startswith("pm25"):
        attrs["pm25_level"] = level
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
    return _status_resource(
        device_id,
        {
            "online": online,
            "reported_at": iso(reported_at),
            "signal_strength": {"value": "good"},
            # Mains powered: the Air Quality Monitor reports 255 here.
            "battery_status": {"percentage": 255},
            "temperature": reading["temperature"],
            "humidity": reading["humidity"],
            "pm2_5": reading["pm2_5"],
            "co": reading["co"],
        },
    )


def contact_status(device_id: str, faulted: bool, reported_at: float) -> dict:
    return _status_resource(
        device_id,
        {
            "online": True,
            "reported_at": iso(reported_at),
            "signal_strength": {"value": "good"},
            "battery_status": {"percentage": 91},
            "sensor_reporting_state": {"value": "active"},
            "contact_detection": {"faulted": faulted},
            "tamper_detection": {"detected": False},
        },
    )


def camera_status(device_id: str, online: bool, reported_at: float) -> dict:
    return _status_resource(
        device_id,
        {"online": online, "reported_at": iso(reported_at), "motion_detection": {"enabled": True}},
    )


def _related_id(device_id: str, name: str) -> str:
    return device_id.replace("ava1.ring.device.", f"ava1.ring.device.{name}.")


def _status_resource(device_id: str, attributes: dict[str, Any]) -> dict:
    return {
        "data": {
            "type": "device-status",
            "id": _related_id(device_id, "status"),
            "attributes": attributes,
        }
    }


# --- the simulated household ------------------------------------------------

CAMERA_ID = "ava1.ring.device.SIMCAMFRONT1A2B"
AQM_ID = "ava1.ring.device.SIMAQMLIVING9F3C"
ACCOUNT_ID = "ava1.ring.account.SIM7C41"
LOCATION_ID = "5ecd04ff-3d93-4a6e-80e4-035f735a58e4"
LOCATION = {"country": "FR", "state": "Grand Est"}

#: Event History rows per page.
HISTORY_PAGE_SIZE = 100


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
    def _catalogue(self) -> list[dict[str, Any]]:
        """Each device with its related resources, before JSON:API shaping."""
        out: list[dict[str, Any]] = [
            {
                "id": self.camera_id,
                "name": "Front Door",
                "status": camera_status(self.camera_id, self.camera_online(self.clock), self.clock),
                "capabilities": {
                    "video": {"codecs": ["AVC"], "max_resolution": 1080},
                    "motion_detection": {"configurations": ["enabled", "motion_zones"]},
                    "image_enhancements": {"configurations": ["hdr"]},
                },
                "configurations": {
                    "motion_detection": {
                        "enabled": True,
                        "motion_zones": [
                            {"id": "zone-1", "name": "Pavement and road", "enabled": True}
                        ],
                    },
                    "image_enhancements": {"privacy_zones": []},
                },
            },
            {
                "id": self.aqm_id,
                "name": "Living Room Air",
                "status": self.status(self.aqm_id),
                "capabilities": {"battery_status": None},
                "configurations": {
                    "temperature_threshold": {"high": 32.2, "low": 25.0},
                    "humidity_threshold": {"high": 85.0, "low": 65.0},
                    "motion_detection": None,
                    "image_enhancements": None,
                    "audio": None,
                },
            },
        ]
        for opening_id, label in self.labels.items():
            out.append(
                {
                    "id": opening_id,
                    "name": label,
                    "status": contact_status(
                        opening_id, self.opening_open(opening_id, self.clock), self.clock
                    ),
                    "capabilities": {"battery_status": {"supported": True}},
                    "configurations": {
                        "motion_detection": None,
                        "image_enhancements": None,
                        "audio": None,
                    },
                }
            )
        return out

    def devices(self, include: str = "") -> dict[str, Any]:
        """``GET /v1/devices``: a JSON:API compound document.

        Every device is ``{"type": "devices", "id", "attributes": {"name"},
        "relationships": {...}}``. The resources named in ``include`` are
        returned once each in the top-level ``included`` array and matched by
        their ``(type, id)`` pair, as the documentation describes.
        """
        wanted = {part.strip() for part in include.split(",") if part.strip()}
        data: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        for dev in self._catalogue():
            did = dev["id"]
            rels: dict[str, Any] = {}
            for name in ("status", "capabilities", "configurations", "location"):
                rtype = "locations" if name == "location" else f"device-{name}"
                rid = LOCATION_ID if name == "location" else _related_id(did, name)
                rels[name] = {
                    "data": {"type": rtype, "id": rid},
                    "links": {"related": f"/v1/devices/{did}/{name}"},
                }
                if name in wanted:
                    if name == "status":
                        attrs = dev["status"]["data"]["attributes"]
                    elif name == "location":
                        attrs = dict(LOCATION)
                    else:
                        attrs = dev[name]
                    included.append({"type": rtype, "id": rid, "attributes": attrs})
            data.append(
                {
                    "type": "devices",
                    "id": did,
                    "attributes": {
                        "name": dev["name"],
                        "image_url": f"https://images.example.invalid/{did}.png",
                    },
                    "relationships": rels,
                }
            )
        out: dict[str, Any] = {"meta": {"time": iso(self.clock)}, "data": data}
        if included:
            out["included"] = included
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

    def history(self, end: float) -> list[dict[str, Any]]:
        """The camera's Event History up to ``end``, newest first.

        Every motion type the webhooks carry is listed, whatever filter was
        asked for: ``event_type`` joins kind and sub-type with a dot, and
        ``start`` and ``end`` are epoch milliseconds.
        """
        events = [(stamp, "motion.vehicle") for stamp in self.result.vehicle_event_times]
        events += [(stamp + 3, "motion.human") for stamp in self.result.vehicle_event_times[::7]]
        rows = []
        for stamp, kind in sorted(events, reverse=True):
            if stamp <= end:
                rows.append(
                    {
                        "type": "history-events",
                        "id": f"hist-{kind}-{int(stamp * 1000)}",
                        "attributes": {
                            "event_type": kind,
                            "start": int(stamp * 1000),
                            "end": int((stamp + 12) * 1000),
                        },
                        "relationships": {
                            "source": {"data": {"type": "devices", "id": self.camera_id}}
                        },
                    }
                )
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


class RateLimitHeaders:
    """The documented ``X-RateLimit-*`` headers, 100 requests per second.

    A plain ASGI wrapper: the seed replays twenty thousand status reads through
    this server, and it adds nothing to each one but two headers.
    """

    def __init__(self, app: Any, counter: Any):
        self.app = app
        self.counter = counter

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.counter.calls += 1
        remaining = str(max(0, 100 - self.counter.calls % 100)).encode()

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"x-ratelimit-limit", b"100"),
                    (b"x-ratelimit-remaining", remaining),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)



def create_app(home: SimHome, hmac_key: str, webhook_url: str | None = None):
    """A FastAPI app serving the documented Ring endpoints for ``home``."""
    import hashlib
    import hmac as hmac_mod

    import httpx
    from fastapi import FastAPI, Query, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Ring Partner API (local simulator)", version="2026-08-24")
    app.state.home = home
    app.state.webhook_url = webhook_url
    app.state.delivered = 0
    app.state.calls = 0
    app.add_middleware(RateLimitHeaders, counter=app.state)

    def _sign(body: bytes) -> str:
        return "sha256=" + hmac_mod.new(hmac_key.encode(), body, hashlib.sha256).hexdigest()

    @app.post("/oauth/token")
    async def token() -> dict:
        return {
            "access_token": "sim-" + uuid.uuid4().hex,
            "refresh_token": "sim-refresh-" + uuid.uuid4().hex,
            "expires_in": 14400,
            "token_type": "Bearer",
            "scope": "ava",
            "account_id": home.account_id,
        }

    @app.get("/v1/users/me")
    async def me() -> dict:
        return {
            "data": {
                "type": "users",
                "id": home.account_id,
                "attributes": {"first_name": "Sim", "last_name": "Household"},
            }
        }

    @app.get("/v1/devices")
    async def devices(include: str = "") -> dict:
        return home.devices(include)

    @app.get("/v1/devices/{device_id}")
    async def device(device_id: str) -> Any:
        for entry in home.devices()["data"]:
            if entry["id"] == device_id:
                return {"data": entry}
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/status")
    async def status(device_id: str) -> Any:
        try:
            return home.status(device_id)
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)

    def _related(device_id: str, name: str) -> Any:
        for entry in home._catalogue():
            if entry["id"] == device_id:
                return {
                    "data": {
                        "type": f"device-{name}",
                        "id": _related_id(device_id, name),
                        "attributes": entry[name],
                    }
                }
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/v1/devices/{device_id}/capabilities")
    async def capabilities(device_id: str) -> Any:
        return _related(device_id, "capabilities")

    @app.get("/v1/devices/{device_id}/configurations")
    async def configurations(device_id: str) -> Any:
        return _related(device_id, "configurations")

    @app.get("/v1/devices/{device_id}/location")
    async def location(device_id: str) -> Any:
        return {"data": {"type": "locations", "id": LOCATION_ID, "attributes": dict(LOCATION)}}

    @app.get("/v1/history/devices/{device_id}/events")
    async def history(device_id: str, page_key: str = Query("0", alias="page[key]")) -> Any:
        """Paged by ``page[key]``. ``event_types`` is accepted and ignored, as
        the Playground does, and ``links.next`` is always present: the page
        after the last one is empty."""
        if device_id != home.camera_id:
            return JSONResponse({"error": "not_found"}, status_code=404)
        rows = home.history(home.clock)
        offset = int(page_key or 0)
        nxt = offset + HISTORY_PAGE_SIZE
        return {
            "data": rows[offset:nxt],
            "links": {"next": f"/v1/history/devices/{device_id}/events?page[key]={nxt}"},
        }

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
        """Deliver a signed vehicle-motion webhook stamped now."""
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
