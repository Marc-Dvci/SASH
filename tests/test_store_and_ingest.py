"""Storage rules that decide whether the model sees measurements or artefacts.

The Air Quality Monitor reports about every fifteen minutes and
``GET /v1/devices/{id}/status`` returns the *last reported* reading. Poll it
every minute and fourteen of those responses are the same measurement. Storing
them as fifteen measurements would flatten every decay in the record and make
the home look far leakier than it is, so the store refuses a repeated
``reported_at`` and the poller counts the refusal as a health signal.
"""

from __future__ import annotations

import httpx
import pytest

from sash.config import RingConfig
from sash.ingest import AirQualityPoller, EventProcessor, IngestStats, Pipeline
from sash.ring.auth import StaticTokenService
from sash.ring.client import RingClient
from simulator.ring_api import create_app
from simulator.seed import build_home


def test_a_repeated_reported_at_is_refused(store):
    store.upsert_device("aqm", "Air", "air_quality_monitor")
    assert store.add_aq_sample("aqm", 1000.0, 1005.0, 5.0, 0.1, 20.0, 45.0)
    assert not store.add_aq_sample("aqm", 1000.0, 1400.0, 5.0, 0.1, 20.0, 45.0)
    assert len(store.aq_samples("aqm")) == 1


def test_a_new_reported_at_is_stored(store):
    store.upsert_device("aqm", "Air", "air_quality_monitor")
    store.add_aq_sample("aqm", 1000.0, 1005.0, 5.0, 0.1, 20.0, 45.0)
    store.add_aq_sample("aqm", 1900.0, 1905.0, 6.0, 0.1, 20.0, 45.0)
    assert [r["reported_at"] for r in store.aq_samples("aqm")] == [1000.0, 1900.0]


def test_a_duplicate_request_id_is_seen_once(store):
    assert store.record_receipt("r1", "motion_detected", "cam")
    assert not store.record_receipt("r1", "motion_detected", "cam")
    assert store.receipt_count() == 1


@pytest.mark.asyncio
async def test_polling_faster_than_the_device_reports_yields_repeats_not_samples():
    """Six polls across one reporting slot must produce one measurement."""
    home = build_home(days=1.5, seed=11, end=1_760_000_000.0)
    app = create_app(home, "k")
    config = RingConfig()
    config.api_base_url = "http://sim"

    from sash.store import Store

    store = Store(":memory:")
    store.upsert_device(home.aqm_id, "Air", "air_quality_monitor")
    store.set_role(home.aqm_id, "indoor_air", "Air", None)

    stats = IngestStats()
    async with RingClient(
        config, StaticTokenService("t").bearer, transport=httpx.ASGITransport(app=app)
    ) as client:
        poller = AirQualityPoller(store, client, stats, clock=lambda: home.clock)
        report = home.result.aqm_reports[4]
        for offset in (30.0, 120.0, 300.0, 480.0, 660.0, 840.0):
            home.clock = report["reported_at"] + offset
            await poller.poll_once()

    assert stats.aq_polls == 6
    assert stats.aq_new_samples == 1
    assert stats.aq_repeat_readings == 5
    store.close()


@pytest.mark.asyncio
async def test_the_pipeline_drains_what_the_receiver_hands_it(store):
    """The webhook route may only enqueue; the work happens behind it."""
    from sash.ring.webhook import WebhookReceiver, sign
    from simulator.ring_api import motion_payload
    import json

    store.upsert_device("cam", "Front", "camera")
    store.set_role("cam", "street_camera", "Front", "street")
    processor = EventProcessor(store)
    pipeline = Pipeline(processor)
    receiver = WebhookReceiver("k", store, pipeline)

    for i in range(20):
        payload = motion_payload("cam", 1_760_000_000.0 + i, "vehicle")
        raw = json.dumps(payload).encode()
        receiver.handle(raw, sign(raw, "k"), 0.0)

    assert pipeline.depth() == 20
    assert store.vehicle_event_count() == 0, "nothing may be applied in the request path"

    import asyncio

    task = asyncio.create_task(pipeline.run())
    await pipeline.drain()
    pipeline.stop()
    task.cancel()

    assert store.vehicle_event_count() == 20
    assert pipeline.depth() == 0


def test_a_role_is_application_side_not_ring_side(store):
    """Ring reports a contact sensor's state, never what it is attached to."""
    store.upsert_device("win", "Sensor 3", "contact_sensor")
    assert store.device("win").app_role in (None, "unused")
    store.set_role("win", "opening", "Courtyard window", "courtyard")
    device = store.device("win")
    assert device.app_role == "opening"
    assert device.label == "Courtyard window"
    assert device.orientation == "courtyard"
    assert [d.device_id for d in store.devices(role="opening")] == ["win"]
