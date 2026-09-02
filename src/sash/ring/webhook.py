"""Ring webhook verification.

Ring signs each delivery with HMAC-SHA256 in ``X-Signature`` and expects a 200
within five seconds. The two rules that follow from that:

1. Verify on the **raw bytes**. Re-serialising the parsed JSON changes key order
   and whitespace and the signature stops matching.
2. Do almost nothing in the request path. Verify, dedupe, hand the event to the
   queue, return. In the AWS deployment the queue is SQS; locally it is an
   in-process asyncio queue with the same interface.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from . import events as ev
from .events import RingEvent, RingWebhookError


class SignatureInvalid(ValueError):
    """The delivery did not carry a signature this key could have produced."""


def sign(body: bytes, key: str) -> str:
    """Produce the header value Ring would send for ``body``."""
    digest = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(body: bytes, header: str | None, key: str) -> None:
    """Constant-time signature check. Raises :class:`SignatureInvalid`."""
    if not header:
        raise SignatureInvalid("missing X-Signature header")
    candidate = header.strip()
    expected = sign(body, key)
    # Accept both the prefixed and bare hex forms; Ring documents the prefixed
    # one, but tolerating the bare digest costs nothing and has bitten other
    # integrations during header rewrites at a proxy.
    bare = expected.split("=", 1)[1]
    if not (hmac.compare_digest(candidate, expected) or hmac.compare_digest(candidate, bare)):
        raise SignatureInvalid("X-Signature does not match the signing key")


@dataclass
class WebhookOutcome:
    accepted: bool
    reason: str
    event: RingEvent | None = None


class WebhookReceiver:
    """Verify, dedupe, and enqueue. Nothing else happens here."""

    def __init__(self, hmac_key: str, store: Any, sink: Any):
        self.hmac_key = hmac_key
        self.store = store
        self.sink = sink  # anything with .put_nowait(RingEvent)
        self.stats = {
            "received": 0,
            "invalid_signature": 0,
            "malformed": 0,
            "duplicate": 0,
            "accepted": 0,
        }

    def handle(self, body: bytes, signature: str | None, received_at: float) -> WebhookOutcome:
        self.stats["received"] += 1
        try:
            verify(body, signature, self.hmac_key)
        except SignatureInvalid as exc:
            self.stats["invalid_signature"] += 1
            return WebhookOutcome(False, str(exc))

        try:
            payload = json.loads(body)
            event = ev.parse(payload, received_at)
        except (json.JSONDecodeError, RingWebhookError) as exc:
            self.stats["malformed"] += 1
            return WebhookOutcome(False, f"malformed payload: {exc}")

        first_time = self.store.record_receipt(
            event.request_id, event.event_type, event.device_id
        )
        if not first_time:
            self.stats["duplicate"] += 1
            # A duplicate is a success from Ring's point of view: acknowledge it.
            return WebhookOutcome(True, "duplicate request_id, already processed", event)

        self.sink.put_nowait(event)
        self.stats["accepted"] += 1
        return WebhookOutcome(True, "queued", event)
