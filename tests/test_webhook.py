"""The front door: signature, idempotency, envelope shape, and the vocabulary.

Every case here is something that would be silently wrong rather than loudly
broken, which is the only kind of bug worth a test in a webhook receiver.
"""

from __future__ import annotations

import json

import pytest

from sash.ingest import EventProcessor
from sash.ring import events as ev
from sash.ring.webhook import SignatureInvalid, WebhookReceiver, sign, verify
from simulator.ring_api import contact_payload, lifecycle_payload, motion_payload

HMAC_KEY = "test-signing-key"



class Sink:
    def __init__(self):
        self.events = []

    def put_nowait(self, event):
        self.events.append(event)


def deliver(receiver, payload, *, key=HMAC_KEY, at=1_760_000_000.0, mangle=None):
    raw = json.dumps(payload).encode()
    signature = sign(raw, key)
    if mangle:
        raw = mangle(raw)
    return receiver.handle(raw, signature, at)


# --- signature --------------------------------------------------------------


def test_valid_signature_is_accepted(store):
    sink = Sink()
    r = WebhookReceiver(HMAC_KEY, store, sink)
    out = deliver(r, motion_payload("cam", 1_760_000_000.0, "vehicle"))
    assert out.accepted and len(sink.events) == 1


def test_wrong_key_is_rejected(store):
    sink = Sink()
    r = WebhookReceiver(HMAC_KEY, store, sink)
    out = deliver(r, motion_payload("cam", 1_760_000_000.0, "vehicle"), key="not-the-key")
    assert not out.accepted and "signature" in out.reason.lower()
    assert sink.events == []


def test_missing_signature_header_is_rejected(store):
    r = WebhookReceiver(HMAC_KEY, store, Sink())
    out = r.handle(b'{"meta":{"request_id":"x"},"data":{"type":"motion_detected"}}', None, 0.0)
    assert not out.accepted


def test_signature_is_over_raw_bytes_not_reserialised_json():
    """Re-serialising reorders keys and whitespace; the digest must not survive it."""
    payload = {"meta": {"request_id": "a"}, "data": {"type": "motion_detected"}}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = sign(raw, HMAC_KEY)
    verify(raw, signature, HMAC_KEY)
    with pytest.raises(SignatureInvalid):
        verify(json.dumps(payload, indent=2).encode(), signature, HMAC_KEY)


def test_bare_hex_signature_is_tolerated():
    raw = b"{}"
    prefixed = sign(raw, HMAC_KEY)
    verify(raw, prefixed.split("=", 1)[1], HMAC_KEY)


# --- idempotency ------------------------------------------------------------


def test_duplicate_request_id_is_acknowledged_but_not_reprocessed(store):
    sink = Sink()
    r = WebhookReceiver(HMAC_KEY, store, sink)
    payload = motion_payload("cam", 1_760_000_000.0, "vehicle")
    first = deliver(r, payload)
    second = deliver(r, payload)
    assert first.accepted and second.accepted
    assert "duplicate" in second.reason
    assert len(sink.events) == 1
    assert r.stats["duplicate"] == 1


# --- malformed --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"type": "motion_detected"}},  # no request_id
        {"meta": {"request_id": "x"}},  # no data.type
        {"meta": "not-an-object", "data": {"type": "motion_detected"}},
    ],
)
def test_malformed_envelopes_are_refused(store, payload):
    r = WebhookReceiver(HMAC_KEY, store, Sink())
    out = deliver(r, payload)
    assert not out.accepted and "malformed" in out.reason


def test_non_json_body_is_refused(store):
    r = WebhookReceiver(HMAC_KEY, store, Sink())
    raw = b"not json at all"
    out = r.handle(raw, sign(raw, HMAC_KEY), 0.0)
    assert not out.accepted


def test_attributes_are_read_from_either_documented_envelope_shape():
    nested = {
        "meta": {"request_id": "a"},
        "data": {"type": "motion_detected", "attributes": {"sub_type": "vehicle"}},
    }
    flat = {
        "meta": {"request_id": "b"},
        "data": {"type": "motion_detected"},
        "attributes": {"sub_type": "vehicle"},
    }
    assert ev.parse(nested, 0.0).sub_type == "vehicle"
    assert ev.parse(flat, 0.0).sub_type == "vehicle"


def test_timestamp_in_milliseconds_is_converted():
    payload = motion_payload("cam", 1_760_000_000.0, "vehicle")
    assert ev.parse(payload, 0.0).timestamp == pytest.approx(1_760_000_000.0, abs=1.0)


# --- the vocabulary ---------------------------------------------------------


def test_contact_faulted_means_open():
    """Ring's contact vocabulary is inverted relative to the physical opening.

    Reading it the intuitive way inverts every downstream estimate without
    raising anything, so it is pinned here and used nowhere else.
    """
    assert ev.contact_state(ev.CONTACT_FAULTED) == "open"
    assert ev.contact_state(ev.CONTACT_CLEARED) == "closed"
    assert ev.contact_state("motion_detected") is None


def test_contact_transitions_reach_the_store_with_the_right_state(store):
    store.upsert_device("win", "Window", "contact_sensor")
    store.set_role("win", "opening", "Window", "street")
    processor = EventProcessor(store)
    r = WebhookReceiver(HMAC_KEY, store, processor_sink(processor))
    deliver(r, contact_payload("win", 1_760_000_000.0, True))
    deliver(r, contact_payload("win", 1_760_003_600.0, False))
    assert [state for _, _, state in store.contact_events("win")] == ["open", "closed"]


def processor_sink(processor):
    class _S:
        def put_nowait(self, event):
            processor.apply(event)

    return _S()


# --- what SASH refuses to keep ---------------------------------------------


def test_human_motion_is_discarded_at_the_door(store):
    """SASH asks for vehicle motion. Human motion arrives anyway and is dropped.

    Ring's grant is per device, not per sub-type, so a camera scope delivers
    people as well as cars. Keeping them would be a record of when the household
    comes and goes, which the product has no use for.
    """
    store.upsert_device("cam", "Front", "camera")
    store.set_role("cam", "street_camera", "Front", "street")
    processor = EventProcessor(store)
    r = WebhookReceiver(HMAC_KEY, store, processor_sink(processor))
    for sub_type in ("vehicle", "human", "motion", "other_motion"):
        deliver(r, motion_payload("cam", 1_760_000_100.0, sub_type))
    assert store.vehicle_event_count() == 1
    assert processor.stats.motion_discarded == 3
    assert processor.stats.motion_unclassified == 1


def test_smart_alerts_off_is_said_on_the_setup_tab_not_dropped_silently(store):
    """With the owner's Smart Alerts off, Ring sends every motion as ``motion``.

    None of it is a vehicle, so none of it is stored, and the street goes quiet.
    SASH keeps a count and the hour of the latest, and the health check names
    the cause instead of blaming the motion zone.
    """
    from sash.service import Household

    store.upsert_device("cam", "Front", "camera")
    store.set_role("cam", "street_camera", "Front", "street")
    processor = EventProcessor(store)
    r = WebhookReceiver(HMAC_KEY, store, processor_sink(processor))
    stamps = [1_760_000_100.0 + 600.0 * i for i in range(5)]
    for t in stamps:
        deliver(r, motion_payload("cam", t, "motion"))
    assert store.vehicle_event_count() == 0
    assert processor.stats.motion_unclassified == 5
    assert store.get_meta("unclassified_motion") == {
        "count": 5,
        "last_hour": stamps[-1] - stamps[-1] % 3600,
    }

    checks = Household(store, clock=lambda: stamps[-1] + 3600.0).health()["checks"]
    (vehicle,) = [c for c in checks if c["name"] == "Vehicle-motion events arriving"]
    assert vehicle["ok"] is False
    assert "Smart Alerts" in vehicle["detail"] and "5 events" in vehicle["detail"]

    # Two days on with nothing unclassified since, the old cause is not repeated.
    checks = Household(store, clock=lambda: stamps[-1] + 2 * 86400.0).health()["checks"]
    (vehicle,) = [c for c in checks if c["name"] == "Vehicle-motion events arriving"]
    assert "Smart Alerts" not in vehicle["detail"]


def test_device_offline_is_recorded_so_a_quiet_street_is_not_invented(store):
    store.upsert_device("cam", "Front", "camera")
    processor = EventProcessor(store)
    r = WebhookReceiver(HMAC_KEY, store, processor_sink(processor))
    deliver(r, lifecycle_payload("cam", 1_760_000_000.0, "device_offline"))
    deliver(r, lifecycle_payload("cam", 1_760_003_600.0, "device_online"))
    assert store.health_transitions("cam") == [
        (1_760_000_000.0, False),
        (1_760_003_600.0, True),
    ]
