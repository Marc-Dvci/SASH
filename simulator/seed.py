"""Replay a simulated household's history through the product's own code path.

Nothing here writes to the database directly. Every vehicle event, every contact
transition and every threshold crossing is built as a Ring webhook payload,
signed with the HMAC key, and handed to :class:`sash.ring.webhook.WebhookReceiver`
exactly as Ring would deliver it. Every PM2.5 reading is fetched by
:class:`sash.ingest.AirQualityPoller` calling ``GET /v1/devices/{id}/status``
through the real :class:`sash.ring.client.RingClient`, over an in-process
transport to the simulator app.

So the seeded history is synthetic -- and it is labelled synthetic everywhere it
is shown -- but the pipeline that produced it is the shipping one. A bug in
signature verification, in idempotency, in the ``reported_at`` dedupe or in the
inverted contact vocabulary shows up here, on the first run, rather than on the
first real household.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from sash.config import RingConfig
from sash.ingest import AirQualityPoller, EventProcessor, IngestStats, discover_devices
from sash.ring.auth import StaticTokenService
from sash.ring.client import RingClient
from sash.ring.webhook import WebhookReceiver, sign
from sash.store import Store

from .home import HomeParams, default_home, simulate
from .ring_api import SimHome, create_app


class _DirectSink:
    """Stands in for SQS: applies each event as it is queued."""

    def __init__(self, processor: EventProcessor):
        self.processor = processor
        self.count = 0

    def put_nowait(self, event: Any) -> None:
        self.processor.apply(event)
        self.count += 1


@dataclass
class SeedReport:
    devices: int
    webhooks_delivered: int
    webhooks_accepted: int
    webhooks_rejected: int
    duplicates_ignored: int
    vehicle_events: int
    motion_discarded: int
    contact_events: int
    threshold_events: int
    aq_polls: int
    aq_samples: int
    aq_repeats: int
    seconds: float

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def build_home(
    *, days: float = 60.0, seed: int = 7, params: HomeParams | None = None, end: float | None = None
) -> SimHome:
    """Simulate a household ending ``now`` (or at ``end``)."""
    params = params or default_home()
    end = end if end is not None else time.time()
    t0 = end - days * 86400.0
    result = simulate(params, days=days, t0=t0, seed=seed)
    labels = {o.opening_id: o.label for o in params.openings}
    orientations = {o.opening_id: o.orientation for o in params.openings}
    return SimHome(
        result=result, labels=labels, params=params, orientations=orientations, clock=t0
    )


async def seed_store(
    store: Store,
    home: SimHome,
    *,
    hmac_key: str = "sash-dev-key",
    chunk_hours: float = 6.0,
    duplicate_every: int = 500,
    until: float | None = None,
    progress: Callable[[float, int, int], None] | None = None,
) -> SeedReport:
    """Drive the simulated history into ``store`` through the real pipeline.

    ``until`` stops the replay short of the end of the tape, which is how the
    demo keeps a stretch of the household's life unplayed: the rest arrives
    live, one Advance at a time, through the same webhook route.

    ``progress`` is called after each chunk with ``(fraction, webhooks, samples)``.
    Four months of a household is eighty thousand signed webhooks and twenty
    thousand status polls, and every one of them goes through the code that
    would face Ring, so this takes minutes rather than seconds. A caller with a
    terminal should say so.
    """
    started = time.time()
    store.reset()

    app = create_app(home, hmac_key)
    config = RingConfig()
    config.api_base_url = "http://sim"
    transport = httpx.ASGITransport(app=app)
    tokens = StaticTokenService("sim-token")

    processor = EventProcessor(store)
    sink = _DirectSink(processor)
    receiver = WebhookReceiver(hmac_key, store, sink)

    async with RingClient(config, tokens.bearer, transport=transport) as client:
        home.clock = home.result.t0 + home.result.minutes * 60.0
        await discover_devices(store, client)
        _assign_roles(store, home)

        stats: IngestStats = processor.stats
        poller = AirQualityPoller(store, client, stats, clock=lambda: home.clock)

        # ``events_between`` is half-open on the left, the way a delivery cursor
        # has to be, so the replay starts a second before the tape does.
        # Otherwise an opening that is already open at minute zero never gets its
        # open webhook, and the record carries a close with nothing before it.
        t0 = home.result.t0 - 1.0
        end = home.result.t0 + home.result.minutes * 60.0
        if until is not None:
            end = min(end, until)
        chunk = chunk_hours * 3600.0
        delivered = 0
        cursor = t0
        report_index = 0
        reports = home.result.aqm_reports

        while cursor < end:
            chunk_end = min(cursor + chunk, end)
            for payload in home.events_between(cursor, chunk_end):
                raw = json.dumps(payload).encode()
                receiver.handle(raw, sign(raw, hmac_key), payload["data"]["attributes"]["timestamp"] / 1000.0)
                delivered += 1
                # Ring retries on a slow acknowledgement, so the same
                # request_id arrives twice. Prove the dedupe on real traffic.
                if duplicate_every and delivered % duplicate_every == 0:
                    receiver.handle(raw, sign(raw, hmac_key), time.time())
                    delivered += 1

            # Poll the Air Quality Monitor the way the scheduler would: after
            # each reporting slot, with the clock where it actually was.
            while report_index < len(reports) and reports[report_index]["reported_at"] <= chunk_end:
                # Two polls per reporting slot: one that finds a new reading and
                # one that finds the same one again. The second is the case the
                # store has to refuse, and it is the common case in production.
                home.clock = reports[report_index]["reported_at"] + 45.0
                await poller.poll_once()
                home.clock = reports[report_index]["reported_at"] + 8 * 60.0
                await poller.poll_once()
                report_index += 1

            cursor = chunk_end
            if progress is not None:
                progress(
                    (cursor - t0) / max(end - t0, 1e-9),
                    delivered,
                    processor.stats.aq_new_samples,
                )

        home.clock = end

    return SeedReport(
        devices=len(store.devices()),
        webhooks_delivered=delivered,
        webhooks_accepted=receiver.stats["accepted"],
        webhooks_rejected=receiver.stats["invalid_signature"] + receiver.stats["malformed"],
        duplicates_ignored=receiver.stats["duplicate"],
        vehicle_events=processor.stats.vehicle,
        motion_discarded=processor.stats.motion_discarded,
        contact_events=processor.stats.contact,
        threshold_events=processor.stats.threshold,
        aq_polls=processor.stats.aq_polls,
        aq_samples=processor.stats.aq_new_samples,
        aq_repeats=processor.stats.aq_repeat_readings,
        seconds=round(time.time() - started, 2),
    )


def _assign_roles(store: Store, home: SimHome) -> None:
    """The mapping a household would do during setup.

    Ring cannot say what a contact sensor is attached to, and its own
    documentation tells integrators to keep that meaning application-side. In the
    product this is four taps in onboarding; here it is seeded so the demo starts
    with a configured home.
    """
    store.set_role(home.camera_id, "street_camera", "Front Door camera", "street")
    store.set_role(home.aqm_id, "indoor_air", "Living Room Air", None)
    for opening_id, label in home.labels.items():
        store.set_role(
            opening_id, "opening", label, home.orientations.get(opening_id, "unknown")
        )
    store.set_meta("seeded_synthetic", True)
    store.set_meta("seed_note", "History in this database was generated by simulator/home.py.")
