"""The four Lambda entry points named in ``infra/template.yaml``.

Each one is thin on purpose. The rules live in the modules the local product
uses, so there is exactly one implementation of the signature check, the
idempotency key, the inverted contact vocabulary and the repeated-reading
refusal.

``infra/template.yaml`` points at
``sash.aws.webhook_handler.handler`` and friends; the shims at the bottom of
this file give each one its own module path without a second copy of anything.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable

from ..config import RingConfig
from ..ingest import EventProcessor, IngestStats
from ..ring.webhook import WebhookReceiver
from .dynamo_store import DynamoStore


# --- wiring -----------------------------------------------------------------


def _secret(cache: dict[str, Any] = {}) -> dict[str, str]:  # noqa: B006
    """Ring credentials from Secrets Manager, cached for the container's life."""
    if "value" in cache:  # pragma: no cover - trivial
        return cache["value"]
    import boto3  # pragma: no cover

    client = boto3.client("secretsmanager")  # pragma: no cover
    raw = client.get_secret_value(SecretId=os.environ["SASH_SECRET_ARN"])  # pragma: no cover
    cache["value"] = json.loads(raw["SecretString"])  # pragma: no cover
    return cache["value"]  # pragma: no cover


def _store(table: Any | None = None) -> DynamoStore:  # pragma: no cover - needs boto3
    from .dynamo_store import table_from_env

    return DynamoStore(table or table_from_env(), now=time.time(), read_ttl_s=60.0)


def _body(event: dict[str, Any]) -> bytes:
    """The raw request body, exactly as it arrived.

    API Gateway hands the body back as text, or base64 when the integration is
    marked binary. Either way the HMAC has to be computed over these bytes and
    never over re-serialised JSON, which reorders keys and changes whitespace.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return raw.encode("utf-8")


def _header(event: dict[str, Any], name: str) -> str | None:
    headers = event.get("headers") or {}
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


class SqsSink:
    """Stands in for the local asyncio queue. Same ``put_nowait``."""

    def __init__(self, client: Any, queue_url: str):
        self.client = client
        self.queue_url = queue_url
        self.sent = 0

    def put_nowait(self, event: Any) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(event.raw),
            # Ring may deliver the same event twice; the receipt table has
            # already absorbed that, and this keeps a retry inside SQS from
            # being applied twice as well.
            MessageAttributes={
                "request_id": {"DataType": "String", "StringValue": event.request_id}
            },
        )
        self.sent += 1


# --- the Ring half ----------------------------------------------------------


def webhook(
    event: dict[str, Any],
    _context: Any = None,
    *,
    store: Any | None = None,
    sink: Any | None = None,
    hmac_key: str | None = None,
    metric: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """Verify, deduplicate, enqueue, acknowledge. Nothing else.

    Ring expects a 200 inside five seconds and retries otherwise, so a duplicate
    gets a 200 too: from Ring's side a retry that is acknowledged is a retry that
    stops. An invalid signature gets a 401, because a retry of an unsigned body
    will not help anyone and the count of them is worth an alarm.
    """
    if store is None:  # pragma: no cover - needs boto3
        store = _store()
    if sink is None:  # pragma: no cover - needs boto3
        import boto3

        sink = SqsSink(boto3.client("sqs"), os.environ["SASH_QUEUE_URL"])
    if hmac_key is None:  # pragma: no cover - needs boto3
        hmac_key = _secret()["hmac_signing_key"]

    receiver = WebhookReceiver(hmac_key, store, sink)
    outcome = receiver.handle(_body(event), _header(event, "X-Signature"), time.time())

    if metric:
        metric("WebhookInvalidSignature", float(receiver.stats["invalid_signature"]))
        metric("WebhookDuplicate", float(receiver.stats["duplicate"]))

    if not outcome.accepted:
        status = 401 if "signature" in outcome.reason.lower() else 400
        return _reply(status, {"ok": False, "reason": outcome.reason})
    return _reply(200, {"ok": True, "reason": outcome.reason})


def processor(
    event: dict[str, Any], _context: Any = None, *, store: Any | None = None
) -> dict[str, Any]:
    """Apply a batch of queued events with the product's own processor.

    Failures are reported per message rather than per batch, so one malformed
    payload does not send nine good ones back to the queue.
    """
    if store is None:  # pragma: no cover - needs boto3
        store = _store()
    engine = EventProcessor(store)
    from ..ring import events as ev

    failures = []
    for record in event.get("Records", []):
        try:
            payload = json.loads(record["body"])
            engine.apply(ev.parse(payload, time.time()))
        except Exception:  # noqa: BLE001
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}


def poller(
    _event: dict[str, Any] | None = None,
    _context: Any = None,
    *,
    store: Any | None = None,
    client: Any | None = None,
    metric: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """Read each Air Quality Monitor's last reported reading.

    Most of these find a reading that is already stored, which is correct and is
    the point of the conditional write. ``AirQualityNewSamples`` going to zero
    for an hour is the alarm.
    """
    import asyncio

    if store is None:  # pragma: no cover - needs boto3
        store = _store()

    stats = IngestStats()

    async def run() -> None:
        nonlocal client
        from ..ingest import AirQualityPoller
        from ..ring.auth import StaticTokenService
        from ..ring.client import RingClient

        owned = client is None
        if owned:  # pragma: no cover - needs boto3
            config = RingConfig()
            config.api_base_url = os.environ.get(
                "RING_API_BASE_URL", config.api_base_url
            )
            tokens = StaticTokenService(_secret()["access_token"])
            client = RingClient(config, tokens.bearer)
        try:
            await AirQualityPoller(store, client, stats).poll_once()
        finally:
            if owned:  # pragma: no cover
                await client.aclose()

    asyncio.run(run())
    if metric:
        metric("AirQualityNewSamples", float(stats.aq_new_samples))
        metric("AirQualityRepeatReadings", float(stats.aq_repeat_readings))
    return stats.as_dict()


# --- the product half -------------------------------------------------------


def api():  # pragma: no cover - needs mangum and boto3
    """The same FastAPI app as the local product, behind Mangum."""
    from mangum import Mangum

    from ..api import create_app
    from ..service import Household

    store = _store()
    household = Household(store, clock=_clock(store))
    return Mangum(create_app(store, household=household), lifespan="off")


def _clock(store: Any, *, ttl_s: float = 60.0) -> Callable[[], float]:
    """The product's "now" on AWS.

    A table seeded with the demo household carries the instant its tape was
    played to, and "now" is that instant, as in ``sash demo``. A table filled
    by Ring is on the wall clock. The meta key is re-read once a minute, so a
    container started before the table was seeded picks it up.
    """
    state: dict[str, float] = {"read_at": -ttl_s}
    cursor: dict[str, Any] = {"value": None}

    def now() -> float:
        if time.monotonic() - state["read_at"] >= ttl_s:
            cursor["value"] = store.get_meta("demo_cursor")
            state["read_at"] = time.monotonic()
        return float(cursor["value"]) if cursor["value"] else time.time()

    return now


def _reply(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
