"""Ring webhook event vocabulary and payload normalisation.

Every field name here comes from the Ring Partner API documentation; the
envelope is ``{"meta": {...}, "data": {"type": ..., "id": ..., "attributes":
{...}}}``. Ring has shipped webhook payloads with ``attributes`` at the top
level in some examples and nested under ``data`` in others, so
:func:`attributes` accepts both shapes rather than guessing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- camera and doorbell ----------------------------------------------------
MOTION_DETECTED = "motion_detected"
BUTTON_PRESS = "button_press"

#: ``data.attributes.sub_type`` values documented for motion events.
SUBTYPE_MOTION = "motion"
SUBTYPE_HUMAN = "human"
SUBTYPE_VEHICLE = "vehicle"
SUBTYPE_OTHER = "other_motion"

# --- sensors (Early Access, August 2026) ------------------------------------
CONTACT_FAULTED = "contact_sensor_faulted"  # circuit broken -> opening is OPEN
CONTACT_CLEARED = "contact_sensor_cleared"  # circuit restored -> opening is CLOSED

PM25_EXCEEDED = "pm25_exceeded"
PM25_CLEARED = "pm25_cleared"
CO_EXCEEDED = "co_exceeded"
CO_CLEARED = "co_cleared"
TEMPERATURE_EXCEEDED = "temperature_exceeded"
TEMPERATURE_CLEARED = "temperature_cleared"
HUMIDITY_EXCEEDED = "humidity_exceeded"
HUMIDITY_CLEARED = "humidity_cleared"

THRESHOLD_EVENTS = frozenset(
    {
        PM25_EXCEEDED,
        PM25_CLEARED,
        CO_EXCEEDED,
        CO_CLEARED,
        TEMPERATURE_EXCEEDED,
        TEMPERATURE_CLEARED,
        HUMIDITY_EXCEEDED,
        HUMIDITY_CLEARED,
    }
)

# --- device and account lifecycle -------------------------------------------
DEVICE_ADDED = "device_added"
DEVICE_REMOVED = "device_removed"
DEVICE_ONLINE = "device_online"
DEVICE_OFFLINE = "device_offline"
APP_INTEGRATION_ADDED = "app_integration_added"
APP_INTEGRATION_REMOVED = "app_integration_removed"

#: Device families, as SASH labels them. Ring warns that capability keys are
#: shared across families, so the mapping is stored application-side and is
#: never re-derived from a capability probe.
FAMILY_CAMERA = "camera"
FAMILY_CONTACT = "contact_sensor"
FAMILY_AQM = "air_quality_monitor"


class RingWebhookError(ValueError):
    """A payload that does not conform to the documented envelope."""


@dataclass(frozen=True)
class RingEvent:
    """A normalised Ring webhook, independent of envelope shape."""

    request_id: str
    account_id: str | None
    event_type: str
    device_id: str | None
    timestamp: float  # epoch seconds
    sub_type: str | None
    attributes: dict[str, Any]
    raw: dict[str, Any]

    @property
    def is_vehicle_motion(self) -> bool:
        return self.event_type == MOTION_DETECTED and self.sub_type == SUBTYPE_VEHICLE


def attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the event attributes from either documented envelope shape."""
    data = payload.get("data") or {}
    if isinstance(data, dict) and isinstance(data.get("attributes"), dict):
        return dict(data["attributes"])
    if isinstance(payload.get("attributes"), dict):
        return dict(payload["attributes"])
    return {}


def _timestamp(attrs: dict[str, Any], fallback: float) -> float:
    """Ring stamps ``timestamp`` in epoch milliseconds."""
    raw = attrs.get("timestamp")
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    # Anything above this is milliseconds; below it, the payload already used
    # seconds. 1e11 seconds is the year 5138, 1e11 ms is 1973.
    return value / 1000.0 if value > 1e11 else value


def parse(payload: dict[str, Any], received_at: float) -> RingEvent:
    """Normalise a raw webhook body. Raises :class:`RingWebhookError`."""
    if not isinstance(payload, dict):
        raise RingWebhookError("payload is not a JSON object")

    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise RingWebhookError("meta is not an object")

    request_id = meta.get("request_id")
    if not request_id:
        raise RingWebhookError("meta.request_id is required for idempotency")

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RingWebhookError("data is not an object")

    event_type = data.get("type") or payload.get("type")
    if not event_type:
        raise RingWebhookError("data.type is required")

    attrs = attributes(payload)
    device_id = attrs.get("source") or attrs.get("device_id") or data.get("id")

    return RingEvent(
        request_id=str(request_id),
        account_id=meta.get("account_id"),
        event_type=str(event_type),
        device_id=str(device_id) if device_id else None,
        timestamp=_timestamp(attrs, received_at),
        sub_type=attrs.get("sub_type"),
        attributes=attrs,
        raw=payload,
    )


def contact_state(event_type: str) -> str | None:
    """Ring's contact vocabulary is inverted relative to the physical opening.

    ``faulted`` means the magnet and reed switch are apart, i.e. the window is
    open. Getting this backwards silently inverts every downstream estimate, so
    it lives in one function with one test.
    """
    if event_type == CONTACT_FAULTED:
        return "open"
    if event_type == CONTACT_CLEARED:
        return "closed"
    return None
