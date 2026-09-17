"""Ring Partner API client.

Two properties of this client matter more than the endpoints it wraps.

**It cannot reach a camera's media.** Ring's media endpoints (WHEP live view,
MP4 clip download, snapshot download) sit behind the same Cameras and Doorbells
scope SASH needs for ``motion.vehicle`` events, so the grant is broader than the
product. Rather than promising not to use them, the client enforces a path
allowlist: a media path raises :class:`MediaAccessDenied` before a socket is
opened. ``tests/test_no_media.py`` walks the whole documented media surface and
asserts every one of them is refused.

**It treats a repeated reading as a repeat.** ``GET /v1/devices/{id}/status``
returns the last reported reading, not a live one. The client always surfaces
``reported_at`` alongside the values so the caller can tell a new measurement
from the same measurement fetched twice.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..config import RingConfig

#: Paths SASH is allowed to call, as anchored regexes against the request path.
#: Adding to this list is the only way to widen the client's reach, which makes
#: the blast radius of the Ring grant reviewable in one screen.
ALLOWED_PATHS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/v1/devices$",
        r"^/v1/devices/[^/]+$",
        r"^/v1/devices/[^/]+/status$",
        r"^/v1/devices/[^/]+/capabilities$",
        r"^/v1/devices/[^/]+/location$",
        r"^/v1/devices/[^/]+/configurations$",
        r"^/v1/history/devices/[^/]+/events$",
        r"^/v1/users/me$",
        r"^/v1/accounts/me/app-integrations$",
        r"^/v1/accounts/me/subscriptions$",
    )
)

#: Recognised media paths, refused with a specific error rather than a generic
#: "not allowed", so the reason shows up in logs and in the UI.
MEDIA_PATH = re.compile(r"/media/")


class MediaAccessDenied(PermissionError):
    """Raised when any call would touch camera imagery, video or audio."""


class PathNotAllowed(PermissionError):
    """Raised for a path outside the reviewed allowlist."""


class RingAPIError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"Ring API {status} on {path}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


@dataclass
class RateLimitState:
    """Mirror of the ``X-RateLimit-*`` headers, for the health page."""

    limit: int | None = None
    remaining: int | None = None
    last_429_at: float | None = None
    throttled_calls: int = 0


@dataclass
class ClientMetrics:
    calls: int = 0
    errors: int = 0
    media_refusals: int = 0
    by_path: dict[str, int] = field(default_factory=dict)


def check_path(path: str) -> None:
    """Raise unless ``path`` is on the reviewed allowlist."""
    if MEDIA_PATH.search(path):
        raise MediaAccessDenied(
            f"SASH never reads camera media. Refused: {path}. "
            "Vehicle classification arrives as event metadata; the imagery is "
            "not needed and is not fetched."
        )
    for pattern in ALLOWED_PATHS:
        if pattern.match(path):
            return
    raise PathNotAllowed(f"{path} is not on the SASH allowlist")


def sideload(devices: list[dict[str, Any]], included: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join a compound document's ``included`` resources onto their devices.

    Ring returns ``{"type": "device-status", "id": ..., "attributes": {...}}``
    resources in ``included`` and points at them from each device's
    ``relationships.<name>.data``. The join key is the ``(type, id)`` pair. The
    joined attributes land under ``attributes.<relationship>`` on the device.
    """
    by_key = {
        (res.get("type"), res.get("id")): res.get("attributes") or {}
        for res in included
        if isinstance(res, dict)
    }
    out: list[dict[str, Any]] = []
    for device in devices:
        entry = dict(device)
        attrs = dict(entry.get("attributes") or {})
        for name, rel in (entry.get("relationships") or {}).items():
            link = (rel or {}).get("data") or {}
            found = by_key.get((link.get("type"), link.get("id")))
            if found is not None and name not in attrs:
                attrs[name] = found
        entry["attributes"] = attrs
        out.append(entry)
    return out


class RingClient:
    """Async Ring API client.

    ``token_provider`` is an awaitable returning a current bearer token, so the
    client never owns refresh logic (see :mod:`sash.ring.auth`).
    """

    def __init__(
        self,
        config: RingConfig,
        token_provider: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.config = config
        self._token_provider = token_provider
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url, timeout=timeout, transport=transport
        )
        self.rate_limit = RateLimitState()
        self.metrics = ClientMetrics()
        self.max_retries = max_retries

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RingClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # --- transport --------------------------------------------------------
    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            check_path(path)
        except MediaAccessDenied:
            self.metrics.media_refusals += 1
            raise

        token = await self._token_provider()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "SASH/1.0 (+https://github.com/Marc-Dvci/sash)",
        }

        delay = 0.5
        for attempt in range(self.max_retries + 1):
            self.metrics.calls += 1
            self.metrics.by_path[path.split("?")[0]] = (
                self.metrics.by_path.get(path.split("?")[0], 0) + 1
            )
            response = await self._client.request(
                method, path, params=params, json=json_body, headers=headers
            )
            self._absorb_rate_limit_headers(response)

            if response.status_code == 429:
                self.rate_limit.last_429_at = time.time()
                self.rate_limit.throttled_calls += 1
                if attempt == self.max_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                await asyncio.sleep(min(wait, 30.0))
                delay *= 2
                continue

            if 500 <= response.status_code < 600 and attempt < self.max_retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code >= 400:
                self.metrics.errors += 1
                raise RingAPIError(response.status_code, response.text, path)

            if not response.content:
                return None
            return response.json()

        self.metrics.errors += 1
        raise RingAPIError(429, "rate limited after retries", path)

    def _absorb_rate_limit_headers(self, response: httpx.Response) -> None:
        for header, attr in (
            ("X-RateLimit-Limit", "limit"),
            ("X-RateLimit-Remaining", "remaining"),
        ):
            value = response.headers.get(header)
            if value is not None:
                try:
                    setattr(self.rate_limit, attr, int(value))
                except ValueError:
                    pass

    # --- endpoints --------------------------------------------------------
    async def list_devices(self, include: str = "status,capabilities,location,configurations") -> list[dict[str, Any]]:
        """Every device the household shared, with its related resources attached.

        The response is a JSON:API compound document: each device carries
        ``relationships`` naming a status, capabilities, configurations and
        location resource, and the ones asked for with ``include`` arrive in a
        top-level ``included`` array. :func:`sideload` joins them onto the device
        under ``attributes.status``, ``attributes.capabilities`` and so on, which
        is the shape every caller in SASH reads.
        """
        payload = await self.request("GET", "/v1/devices", params={"include": include})
        if isinstance(payload, dict):
            return sideload(list(payload.get("data") or []), payload.get("included") or [])
        return list(payload or [])

    async def device(self, device_id: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/v1/devices/{device_id}")
        return payload.get("data", payload) if isinstance(payload, dict) else {}

    async def device_status(self, device_id: str) -> dict[str, Any]:
        """Last reported status. Never assume it is fresh: read ``reported_at``."""
        payload = await self.request("GET", f"/v1/devices/{device_id}/status")
        return payload.get("data", payload) if isinstance(payload, dict) else {}

    async def device_configurations(self, device_id: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/v1/devices/{device_id}/configurations")
        return payload.get("data", payload) if isinstance(payload, dict) else {}

    async def event_history(
        self,
        device_id: str,
        *,
        event_types: str = "motion.vehicle",
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Camera event history. There is no sensor equivalent of this endpoint."""
        params: dict[str, Any] = {"event_types": event_types, "limit": limit}
        if start_ms is not None:
            params["start_time"] = start_ms
        if end_ms is not None:
            params["end_time"] = end_ms
        payload = await self.request(
            "GET", f"/v1/history/devices/{device_id}/events", params=params
        )
        if isinstance(payload, dict):
            return list(payload.get("data") or [])
        return list(payload or [])

    async def me(self) -> dict[str, Any]:
        payload = await self.request("GET", "/v1/users/me")
        return payload.get("data", payload) if isinstance(payload, dict) else {}

    # --- explicitly refused ----------------------------------------------
    async def download_video(self, device_id: str, *_: Any, **__: Any) -> None:
        """Present so the refusal is discoverable, and permanently refused."""
        await self.request(
            "POST", f"/v1/devices/{device_id}/media/video/download", json_body={}
        )

    async def download_image(self, device_id: str, *_: Any, **__: Any) -> None:
        await self.request(
            "POST", f"/v1/devices/{device_id}/media/image/download", json_body={}
        )

    async def start_live_view(self, device_id: str, *_: Any, **__: Any) -> None:
        await self.request(
            "POST", f"/v1/devices/{device_id}/media/streaming/whep/sessions", json_body={}
        )
