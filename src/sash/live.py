"""SASH against a Ring account.

``sash live`` runs the product with the Ring Partner API at
``api.amazonvision.com`` instead of the bundled simulator. Two ways in, both
through the same :class:`~sash.ring.client.RingClient`:

* ``RING_ACCESS_TOKEN``: a token from the Ring Developer Playground, which
  issues one in a click and needs no app registration. The Playground token is
  valid for thirty minutes, which is enough to discover the account's devices,
  read every status and confirm the poller stores what it reads.
* OAuth: ``RING_CLIENT_ID``, ``RING_CLIENT_SECRET`` and ``RING_REDIRECT_URI``
  from a registered app. ``GET /ring/link`` sends the household to Ring's
  consent page and ``GET /ring/callback`` exchanges the code, after which the
  refresh token keeps the account linked.

Webhooks reach ``POST /ring/webhook`` on whatever public URL the app is
registered with; the receiver is the one the simulator exercises.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import SashConfig
from .ingest import AirQualityPoller, IngestStats, discover_devices
from .ring.auth import RelinkRequired, StaticTokenService, TokenService
from .ring.client import RingClient
from .store import Store


class LiveRing:
    """The Ring side of a running SASH: tokens, discovery and the poller."""

    def __init__(
        self, config: SashConfig, store: Store, stats: IngestStats, *, transport: Any = None
    ):
        self.config = config
        self.store = store
        self.stats = stats
        self.oauth = TokenService(config.ring, store)
        if config.ring.access_token:
            self.tokens: Any = StaticTokenService(config.ring.access_token)
            self.mode = "access_token"
        else:
            self.tokens = self.oauth
            self.mode = "oauth"
        self.client = RingClient(config.ring, self.tokens.bearer, transport=transport)
        self.poller = AirQualityPoller(store, self.client, stats)
        self.account: dict[str, Any] = {}
        self.discovered_at: float | None = None
        self.last_error: str | None = None

    # --- state -------------------------------------------------------------

    @property
    def linked(self) -> bool:
        if self.mode == "access_token":
            return True
        return self.oauth.load() is not None

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "linked": self.linked,
            "api_base_url": self.config.ring.api_base_url,
            "account_id": self.account.get("id"),
            "discovered_at": self.discovered_at,
            "devices": len(self.store.devices()),
            "last_error": self.last_error,
            "can_link": bool(self.config.ring.client_id and self.config.ring.redirect_uri),
            "rate_limit": {
                "limit": self.client.rate_limit.limit,
                "remaining": self.client.rate_limit.remaining,
            },
            "calls": self.client.metrics.calls,
        }

    # --- lifecycle ---------------------------------------------------------

    async def discover(self) -> list[dict[str, Any]]:
        """Read the account and its devices, and record what each one is."""
        try:
            self.account = await self.client.me()
            devices = await discover_devices(self.store, self.client)
            self._default_roles()
            self.discovered_at = time.time()
            self.last_error = None
            return devices
        except RelinkRequired as exc:
            self.last_error = str(exc)
            return []
        except Exception as exc:  # noqa: BLE001 - shown on the Setup tab
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

    def _default_roles(self) -> None:
        """Give newly discovered devices the role their family implies.

        The Air Quality Monitor is the indoor air, a camera watches the street
        and a contact sensor is an opening. Which side an opening faces is the
        household's to say on the Setup tab, so it starts as ``unknown``. A
        role already set is left alone.
        """
        defaults = {
            "air_quality_monitor": "indoor_air",
            "camera": "street_camera",
            "contact_sensor": "opening",
        }
        for device in self.store.devices():
            role = defaults.get(device.family)
            if role and not device.app_role:
                self.store.set_role(
                    device.device_id,
                    role,
                    device.ring_name,
                    "unknown" if role == "opening" else None,
                )

    async def link(self, code: str) -> None:
        """Finish account linking with the code Ring returned, then rediscover."""
        await self.oauth.exchange_code(code)
        await self.discover()

    def start(self, poll_interval_s: float) -> None:
        if self.linked:
            self.poller.start(poll_interval_s)

    async def stop(self) -> None:
        await self.poller.stop()
        await self.client.aclose()


async def spike(config: SashConfig, store: Store, *, transport: Any = None) -> dict[str, Any]:
    """The first minutes with a Ring account, as one command.

    Reads the account, lists its devices, reads every status, and polls each
    Air Quality Monitor twice so the second poll shows the repeated reading
    being refused. Prints what it found and returns it.
    """
    live = LiveRing(config, store, IngestStats(), transport=transport)
    report: dict[str, Any] = {"mode": live.mode, "api_base_url": config.ring.api_base_url}
    try:
        devices = await live.discover()
        report["account"] = live.account.get("id")
        report["devices"] = [
            {
                "id": d.device_id,
                "name": d.ring_name,
                "family": d.family,
                "online": d.online,
            }
            for d in store.devices()
        ]
        statuses = {}
        for entry in devices:
            status = await live.client.device_status(entry["id"])
            statuses[entry["id"]] = (status.get("attributes") or {})
        report["statuses"] = statuses
        first = await live.poller.poll_once()
        await asyncio.sleep(1.0)
        second = await live.poller.poll_once()
        report["poll"] = {
            "new_samples_first_pass": first,
            "new_samples_second_pass": second,
            "repeats_refused": live.stats.aq_repeat_readings,
        }
        report["rate_limit"] = live.describe()["rate_limit"]
        report["calls"] = live.client.metrics.calls
    finally:
        await live.stop()
    return report
