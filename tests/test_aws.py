"""The AWS path, driven against a table double with DynamoDB's semantics.

The stack has never been deployed (see `docs/AWS.md`), so these tests carry the
whole weight of the claim that the handlers are correct. They are written around
the two things that are genuinely different on AWS and would otherwise be
discovered in production:

* **Idempotency is a conditional write, not a read-then-write.** Two Lambdas can
  receive the same Ring retry at the same moment, and a read-then-write lets both
  decide they were first. The double below fails a conditional put exactly the
  way DynamoDB does.
* **The body arrives as text or as base64.** The HMAC has to be computed over the
  bytes that arrived, never over re-serialised JSON.

A memory worth stating here, because it applies: a self-written double agreeing
with your own client is not evidence about the vendor. What these tests prove is
that the handlers implement the semantics they were written against; the first
hour with real credentials still has to confirm those semantics are DynamoDB's.
"""

from __future__ import annotations

import base64
import json

import pytest

from sash.aws.dynamo_store import ConditionalCheckFailed, DynamoStore
from sash.aws.handlers import processor, webhook
from sash.ring.webhook import sign
from simulator.ring_api import contact_payload, motion_payload

HMAC_KEY = "aws-test-key"


class FakeTable:
    """An in-memory table with DynamoDB's conditional-put behaviour."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        key = (Item["pk"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(pk)" and key in self.items:
            raise ConditionalCheckFailed(str(key))
        self.items[key] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    #: Words DynamoDB refuses as bare attribute names in an expression. The
    #: real service rejects the whole call; the double does the same, so a
    #: reserved word reaching an UpdateExpression fails here first.
    RESERVED = {"online", "label", "status", "name", "state", "value", "source", "at"}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):  # noqa: N803
        item = self.items.setdefault((Key["pk"], Key["sk"]), dict(Key))
        names = ExpressionAttributeNames or {}
        assignments = UpdateExpression.replace("SET ", "").split(",")
        for assignment in assignments:
            field, placeholder = (p.strip() for p in assignment.split("="))
            if field.startswith("#"):
                field = names[field]
            elif field in self.RESERVED:
                raise ValueError(f"Invalid UpdateExpression: reserved keyword: {field}")
            item[field] = ExpressionAttributeValues[placeholder]

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **_):  # noqa: N803
        pk = ExpressionAttributeValues[":pk"]
        lo, hi = ExpressionAttributeValues[":lo"], ExpressionAttributeValues[":hi"]
        rows = [
            dict(v)
            for (p, s), v in sorted(self.items.items())
            if p == pk and lo <= s <= hi
        ]
        return {"Items": rows}

    def scan(self, **_):
        return {"Count": len(self.items)}


class FakeSqs:
    def __init__(self):
        self.messages: list[dict] = []

    def send_message(self, QueueUrl, MessageBody, MessageAttributes=None):  # noqa: N803
        self.messages.append({"body": MessageBody, "attrs": MessageAttributes or {}})


@pytest.fixture()
def table():
    return FakeTable()


@pytest.fixture()
def store(table):
    return DynamoStore(table, now=1_760_000_000.0)


def _event(payload, *, key=HMAC_KEY, b64=False):
    raw = json.dumps(payload).encode()
    body = base64.b64encode(raw).decode() if b64 else raw.decode()
    return {
        "body": body,
        "isBase64Encoded": b64,
        "headers": {"X-Signature": sign(raw, key)},
    }


# --- the store -------------------------------------------------------------


def test_a_repeated_request_id_loses_the_conditional_write(store):
    assert store.record_receipt("r1", "motion_detected", "cam") is True
    assert store.record_receipt("r1", "motion_detected", "cam") is False
    assert store.seen_request("r1")


def test_a_receipt_carries_a_ttl(store, table):
    store.record_receipt("r1", "motion_detected", "cam")
    item = table.items[("RECEIPT#r1", "RECEIPT")]
    assert item["expires_at"] > 1_760_000_000


def test_a_repeated_reported_at_is_refused(store):
    assert store.add_aq_sample("aqm", 1000.0, 1005.0, 5.0, 0.1, 20.0, 45.0) is True
    assert store.add_aq_sample("aqm", 1000.0, 1400.0, 9.9, 0.1, 20.0, 45.0) is False
    rows = store.aq_samples("aqm")
    assert len(rows) == 1
    assert rows[0]["pm25"] == 5.0, "the first reading must not be overwritten"


def test_samples_come_back_in_time_order_across_the_padding_boundary(store):
    for t in (1_759_999_999.0, 1_760_000_001.0, 900.0, 86_400.0):
        store.add_aq_sample("aqm", t, t + 5, 1.0, 0.0, 20.0, 45.0)
    times = [r["reported_at"] for r in store.aq_samples("aqm")]
    assert times == sorted(times)


def test_a_range_query_selects_a_window(store):
    for t in range(0, 10):
        store.add_vehicle_event(f"v{t}", "cam", 1_000.0 + t * 100.0, "webhook")
    assert len(store.vehicle_event_times(1_000.0, 1_500.0)) == 5
    assert store.vehicle_event_count() == 10


def test_rediscovery_does_not_wipe_the_household_role_mapping(store):
    store.upsert_device("win", "Sensor 3", "contact_sensor")
    store.set_role("win", "opening", "Courtyard window", "courtyard")
    # Ring is asked for the device list again and knows nothing about placement.
    store.upsert_device("win", "Sensor 3", "contact_sensor")
    device = store.device("win")
    assert device.app_role == "opening"
    assert device.orientation == "courtyard"


def test_contact_events_come_back_in_order_with_their_state(store):
    store.add_contact_event("e2", "win", 2000.0, "closed")
    store.add_contact_event("e1", "win", 1000.0, "open")
    assert store.contact_events("win") == [
        ("win", 1000.0, "open"),
        ("win", 2000.0, "closed"),
    ]


def test_meta_round_trips_a_structure(store):
    store.set_meta("demo_tape", {"days": 120, "seed": 7})
    assert store.get_meta("demo_tape")["seed"] == 7
    assert store.get_meta("missing", "fallback") == "fallback"


def test_a_plan_can_be_written_then_settled(store):
    store.add_plan(
        {
            "plan_id": "p1",
            "created_at": 1_760_000_000.0,
            "opening_id": "win",
            "status": "offered",
            "pred_intake": 300.0,
        }
    )
    store.update_plan("p1", status="verified", verdict="inside_interval")
    row = store.plans()[0]
    assert row["status"] == "verified" and row["verdict"] == "inside_interval"
    assert store.plans(status="offered") == []


# --- the webhook handler ---------------------------------------------------


def test_a_signed_delivery_is_acknowledged_and_queued(store):
    sqs = FakeSqs()
    from sash.aws.handlers import SqsSink

    response = webhook(
        _event(motion_payload("cam", 1_760_000_000.0, "vehicle")),
        store=store,
        sink=SqsSink(sqs, "q"),
        hmac_key=HMAC_KEY,
    )
    assert response["statusCode"] == 200
    assert len(sqs.messages) == 1
    assert sqs.messages[0]["attrs"]["request_id"]["StringValue"]


def test_an_unsigned_delivery_gets_401_and_is_not_queued(store):
    sqs = FakeSqs()
    from sash.aws.handlers import SqsSink

    response = webhook(
        _event(motion_payload("cam", 1_760_000_000.0, "vehicle"), key="wrong"),
        store=store,
        sink=SqsSink(sqs, "q"),
        hmac_key=HMAC_KEY,
    )
    assert response["statusCode"] == 401
    assert sqs.messages == []


def test_a_base64_body_verifies_the_same_as_a_text_one(store):
    """API Gateway hands the body back either way depending on the integration."""
    sqs = FakeSqs()
    from sash.aws.handlers import SqsSink

    payload = motion_payload("cam", 1_760_000_000.0, "vehicle")
    response = webhook(
        _event(payload, b64=True), store=store, sink=SqsSink(sqs, "q"), hmac_key=HMAC_KEY
    )
    assert response["statusCode"] == 200 and len(sqs.messages) == 1


def test_a_retry_is_acknowledged_once_and_queued_once(store):
    """Two Lambdas, one Ring retry. Exactly one message may reach the queue."""
    sqs = FakeSqs()
    from sash.aws.handlers import SqsSink

    event = _event(motion_payload("cam", 1_760_000_000.0, "vehicle"))
    first = webhook(event, store=store, sink=SqsSink(sqs, "q"), hmac_key=HMAC_KEY)
    second = webhook(event, store=store, sink=SqsSink(sqs, "q"), hmac_key=HMAC_KEY)
    assert first["statusCode"] == 200 and second["statusCode"] == 200
    assert "duplicate" in json.loads(second["body"])["reason"]
    assert len(sqs.messages) == 1


def test_a_header_in_any_case_is_found(store):
    sqs = FakeSqs()
    from sash.aws.handlers import SqsSink

    payload = motion_payload("cam", 1_760_000_000.0, "vehicle")
    raw = json.dumps(payload).encode()
    event = {"body": raw.decode(), "headers": {"x-signature": sign(raw, HMAC_KEY)}}
    assert (
        webhook(event, store=store, sink=SqsSink(sqs, "q"), hmac_key=HMAC_KEY)[
            "statusCode"
        ]
        == 200
    )


def test_the_invalid_signature_metric_is_emitted(store):
    from sash.aws.handlers import SqsSink

    seen = {}
    webhook(
        _event(motion_payload("cam", 1.0, "vehicle"), key="wrong"),
        store=store,
        sink=SqsSink(FakeSqs(), "q"),
        hmac_key=HMAC_KEY,
        metric=lambda name, value: seen.__setitem__(name, value),
    )
    assert seen["WebhookInvalidSignature"] == 1.0


# --- the processor handler -------------------------------------------------


def test_the_processor_applies_a_batch_and_keeps_the_contact_vocabulary(store):
    store.upsert_device("win", "Window", "contact_sensor")
    store.set_role("win", "opening", "Window", "street")
    store.upsert_device("cam", "Front", "camera")
    store.set_role("cam", "street_camera", "Front", "street")

    records = [
        {"messageId": "1", "body": json.dumps(contact_payload("win", 1000.0, True))},
        {"messageId": "2", "body": json.dumps(contact_payload("win", 2000.0, False))},
        {"messageId": "3", "body": json.dumps(motion_payload("cam", 3000.0, "vehicle"))},
        {"messageId": "4", "body": json.dumps(motion_payload("cam", 3001.0, "human"))},
    ]
    out = processor({"Records": records}, store=store)
    assert out["batchItemFailures"] == []
    assert [s for _, _, s in store.contact_events("win")] == ["open", "closed"]
    assert store.vehicle_event_count() == 1, "human motion is discarded here too"


def test_one_malformed_message_does_not_fail_the_whole_batch(store):
    records = [
        {"messageId": "bad", "body": "{not json"},
        {"messageId": "good", "body": json.dumps(motion_payload("cam", 3000.0, "vehicle"))},
    ]
    out = processor({"Records": records}, store=store)
    assert out["batchItemFailures"] == [{"itemIdentifier": "bad"}]


# --- the poller ------------------------------------------------------------


def test_the_poller_refuses_a_repeated_reading(store):
    """The common case in production, and the one that would corrupt the model.

    Synchronous on purpose: the handler owns its own event loop, the way a
    Lambda invocation does.
    """
    import asyncio

    import httpx

    from sash.aws.handlers import poller
    from sash.config import RingConfig
    from sash.ring.auth import StaticTokenService
    from sash.ring.client import RingClient
    from simulator.ring_api import create_app
    from simulator.seed import build_home

    home = build_home(days=2.0, seed=3, end=1_760_000_000.0)
    store.upsert_device(home.aqm_id, "Air", "air_quality_monitor")
    store.set_role(home.aqm_id, "indoor_air", "Air", None)

    config = RingConfig()
    config.api_base_url = "http://sim"
    home.clock = home.result.aqm_reports[3]["reported_at"] + 60.0
    client = RingClient(
        config,
        StaticTokenService("t").bearer,
        transport=httpx.ASGITransport(app=create_app(home, "k")),
    )
    try:
        first = poller(store=store, client=client)
        second = poller(store=store, client=client)
    finally:
        asyncio.run(client.aclose())

    assert first["aq_new_samples"] == 1
    assert second["aq_repeat_readings"] == 1
    assert second["aq_new_samples"] == 0


def test_the_deployed_clock_follows_a_seeded_tape(store):
    from sash.aws.handlers import _clock

    now = _clock(store, ttl_s=0.0)
    assert abs(now() - __import__("time").time()) < 5
    store.set_meta("demo_cursor", 1789022700.0)
    assert now() == 1789022700.0
