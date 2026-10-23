"""Event processing and sensor polling.

Two paths bring Ring data into SASH, and they have different failure modes.

**Webhooks** are the only source of sensor events -- Ring publishes no history
endpoint for sensors, so anything not captured when it arrives is gone. The
receiver therefore does the minimum in the request path (verify, dedupe, queue,
return 200 inside Ring's five-second window) and this module drains the queue.
In the AWS deployment the queue is SQS and this module is the consumer Lambda;
locally it is an asyncio queue and a task. The code between them is the same.

**Polling** covers the Air Quality Monitor, whose status endpoint returns the
last reported reading rather than a live one. The device reports roughly every
fifteen minutes, so SASH polls on a shorter cycle and keys every sample on
``reported_at``. A poll that returns the same ``reported_at`` is a health check,
not a measurement, and is recorded as such.
"""

from __future__ import annotations

import asyncio
import bisect
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import AQM_REPORT_INTERVAL_MIN
from .ring import events as ev
from .ring.client import RingClient
from .ring.events import RingEvent
from .store import Store

#: How often the AQ poller wakes. Shorter than the device's reporting cadence so
#: a new reading is picked up promptly; every duplicate is discarded on
#: ``reported_at``.
POLL_INTERVAL_S = 300.0


@dataclass
class IngestStats:
    processed: int = 0
    vehicle: int = 0
    motion_discarded: int = 0
    #: Of the discarded, how many Ring left unclassified (Smart Alerts off).
    motion_unclassified: int = 0
    contact: int = 0
    threshold: int = 0
    lifecycle: int = 0
    unknown: int = 0
    aq_polls: int = 0
    aq_new_samples: int = 0
    aq_repeat_readings: int = 0
    aq_poll_failures: int = 0
    last_event_at: float | None = None
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "by_type"}
        data["by_type"] = dict(self.by_type)
        return data


class EventProcessor:
    """Applies normalised Ring events to the store."""

    def __init__(self, store: Store):
        self.store = store
        self.stats = IngestStats()

    def apply(self, event: RingEvent) -> None:
        self.stats.processed += 1
        self.stats.last_event_at = event.timestamp
        self.stats.by_type[event.event_type] = self.stats.by_type.get(event.event_type, 0) + 1

        handler = {
            ev.MOTION_DETECTED: self._motion,
            ev.CONTACT_FAULTED: self._contact,
            ev.CONTACT_CLEARED: self._contact,
            ev.DEVICE_ONLINE: self._lifecycle,
            ev.DEVICE_OFFLINE: self._lifecycle,
            ev.DEVICE_ADDED: self._lifecycle,
            ev.DEVICE_REMOVED: self._lifecycle,
            ev.APP_INTEGRATION_REMOVED: self._lifecycle,
        }.get(event.event_type)

        if handler is None and event.event_type in ev.THRESHOLD_EVENTS:
            handler = self._threshold
        if handler is None:
            self.stats.unknown += 1
            return
        handler(event)

    # --- handlers ---------------------------------------------------------
    def _motion(self, event: RingEvent) -> None:
        """Only vehicle motion is kept.

        Human and generic motion are discarded at the door. SASH models a
        street, not the people on it, and the cheapest way to guarantee that is
        never to write those events down.

        Unclassified motion is discarded too, but not silently. It is what
        every motion event looks like when the owner's Smart Alerts are off,
        and then no vehicle will ever arrive, so the Setup tab has to be able
        to say why. Only a count and the hour of the latest are kept.
        """
        if event.sub_type != ev.SUBTYPE_VEHICLE:
            self.stats.motion_discarded += 1
            if event.sub_type in ev.UNCLASSIFIED_SUBTYPES:
                self.stats.motion_unclassified += 1
                note_unclassified_motion(self.store, event.timestamp)
            return
        if not event.device_id:
            return
        self.store.add_vehicle_event(
            event.request_id, event.device_id, event.timestamp, "webhook"
        )
        self.stats.vehicle += 1

    def _contact(self, event: RingEvent) -> None:
        state = ev.contact_state(event.event_type)
        if state is None or not event.device_id:
            return
        self.store.add_contact_event(
            event.request_id, event.device_id, event.timestamp, state
        )
        self.stats.contact += 1

    def _threshold(self, event: RingEvent) -> None:
        if not event.device_id:
            return
        level = event.attributes.get("pm25_level")
        if level is None:
            level = event.attributes.get("co_level")
        try:
            level = float(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        self.store.add_threshold_event(
            event.request_id, event.device_id, event.timestamp, event.event_type, level
        )
        self.stats.threshold += 1

    def _lifecycle(self, event: RingEvent) -> None:
        self.stats.lifecycle += 1
        if event.event_type == ev.APP_INTEGRATION_REMOVED:
            # The household withdrew consent. Everything derived from their Ring
            # account goes, and nothing keeps running against it.
            self.store.set_meta("integration_removed_at", event.timestamp)
            self.store.reset()
            return
        if not event.device_id:
            return
        if event.event_type in (ev.DEVICE_ONLINE, ev.DEVICE_OFFLINE):
            self.store.set_online(
                event.device_id, event.event_type == ev.DEVICE_ONLINE, event.timestamp
            )
        elif event.event_type == ev.DEVICE_REMOVED:
            self.store.set_online(event.device_id, False, event.timestamp)


class Pipeline:
    """Queue drain loop, matching the shape of the SQS consumer."""

    def __init__(self, processor: EventProcessor | Store):
        # Accepts a processor, or a store for the convenience of callers that
        # do not need the statistics back.
        self.processor = (
            processor if isinstance(processor, EventProcessor) else EventProcessor(processor)
        )
        self.queue: asyncio.Queue[RingEvent] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = True

    def put_nowait(self, event: RingEvent) -> None:
        self.queue.put_nowait(event)

    def depth(self) -> int:
        return self.queue.qsize()

    async def run(self) -> None:
        while self._running:
            event = await self.queue.get()
            try:
                self.processor.apply(event)
            finally:
                self.queue.task_done()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def drain(self) -> None:
        await self.queue.join()


class AirQualityPoller:
    """Samples the Air Quality Monitor's last reported reading."""

    def __init__(
        self,
        store: Store,
        client: RingClient,
        stats: IngestStats,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.client = client
        self.stats = stats
        #: Source of "now" for the freshness calculation. Injected so a replay
        #: against the simulator measures staleness on the simulated clock
        #: rather than against wall time, which would mark every historical
        #: reading stale and silently produce an empty model.
        self.clock = clock
        self._task: asyncio.Task | None = None

    async def poll_once(self) -> int:
        """Poll every Air Quality Monitor. Returns the number of new samples."""
        new = 0
        for device in self.store.devices(role="indoor_air"):
            self.stats.aq_polls += 1
            try:
                status = await self.client.device_status(device.device_id)
            except Exception:  # noqa: BLE001 - a failed poll is a health signal
                self.stats.aq_poll_failures += 1
                continue
            if self._store_status(device.device_id, status):
                new += 1
        return new

    def _store_status(self, device_id: str, status: dict[str, Any]) -> bool:
        """Store one status resource. Returns True if it was a new reading.

        ``reported_at`` arrives as an ISO 8601 string in ``attributes`` and is
        the time of the reading, not of the response. A device that has not yet
        checked in reports ``null`` or the Unix epoch there, and its placeholder
        values are skipped rather than stored as a measurement.
        """
        attrs = status.get("attributes", status) or {}
        reported_at = ev.epoch_seconds(attrs.get("reported_at", status.get("reported_at")))
        if reported_at is None:
            self.stats.aq_poll_failures += 1
            return False
        stored = self.store.add_aq_sample(
            device_id=device_id,
            reported_at=reported_at,
            fetched_at=self.clock(),
            pm25=_num(attrs.get("pm2_5")),
            co=_num(attrs.get("co")),
            temperature=_num(attrs.get("temperature")),
            humidity=_num(attrs.get("humidity")),
        )
        if stored:
            self.stats.aq_new_samples += 1
        else:
            self.stats.aq_repeat_readings += 1
        return stored

    async def run(self, interval_s: float = POLL_INTERVAL_S) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.stats.aq_poll_failures += 1
            await asyncio.sleep(interval_s)

    def start(self, interval_s: float = POLL_INTERVAL_S) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(interval_s))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def discover_devices(store: Store, client: RingClient) -> list[dict[str, Any]]:
    """Pull the authorised device list and record what each one is.

    Ring warns that capability keys are shared across device families, so the
    family is decided once, here, and stored. Nothing downstream re-derives it.
    """
    devices = await client.list_devices()
    for entry in devices:
        attrs = entry.get("attributes", entry) or {}
        device_id = str(entry.get("id") or attrs.get("id") or "")
        if not device_id:
            continue
        family = _family_of(entry, attrs)
        status = attrs.get("status") if isinstance(attrs.get("status"), dict) else {}
        online = status.get("online", attrs.get("online", True))
        store.upsert_device(
            device_id=device_id,
            ring_name=str(attrs.get("name") or entry.get("name") or device_id),
            family=family,
            online=bool(online),
            config_hash=_config_hash(attrs),
        )
    return devices


def _family_of(entry: dict[str, Any], attrs: dict[str, Any]) -> str:
    """Decide what a device is from what it reports.

    A Ring device resource carries no family field, and Ring warns that
    capability keys are shared across families, so the family is read from the
    sideloaded status and capabilities: a device reporting ``pm2_5`` is the Air
    Quality Monitor, one reporting ``contact_detection`` is a contact sensor,
    and one declaring ``video`` is a camera. The result is advisory. The role a
    device plays in this home is set on the Setup tab and stored beside it.
    """
    status = attrs.get("status") or {}
    caps = attrs.get("capabilities") or entry.get("capabilities") or {}
    if isinstance(status, dict):
        if "pm2_5" in status or "co" in status:
            return ev.FAMILY_AQM
        if "contact_detection" in status:
            return ev.FAMILY_CONTACT
    if isinstance(caps, dict):
        if "pm2_5" in caps or "air_quality" in caps:
            return ev.FAMILY_AQM
        if "contact_detection" in caps:
            return ev.FAMILY_CONTACT
        if "video" in caps or "live_view" in caps:
            return ev.FAMILY_CAMERA
    declared = str(entry.get("device_type") or attrs.get("device_type") or "").lower()
    if "air" in declared or "aqm" in declared:
        return ev.FAMILY_AQM
    if "contact" in declared:
        return ev.FAMILY_CONTACT
    if "camera" in declared or "doorbell" in declared:
        return ev.FAMILY_CAMERA
    if isinstance(caps, dict) and "motion_detection" in caps:
        return ev.FAMILY_CAMERA
    return ev.FAMILY_SENSOR


def _config_hash(attrs: dict[str, Any]) -> str | None:
    """Fingerprint of the motion-zone configuration.

    A camera whose detection zone changes is a different instrument. Counts
    either side of that change are not comparable, so the hash marks a new
    calibration epoch rather than silently mixing them.
    """
    import hashlib
    import json

    config = attrs.get("configurations") or attrs.get("motion_zones")
    if config is None:
        return None
    blob = json.dumps(config, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


#: Meta key for unclassified motion: a count and the hour of the latest.
UNCLASSIFIED_MOTION_KEY = "unclassified_motion"

#: A history row this close to a vehicle event already on record is that event,
#: delivered once by webhook and listed again by Event History.
HISTORY_DEDUPE_S = 10.0

#: Pages of Event History read per backfill, at most.
HISTORY_MAX_PAGES = 20


def note_unclassified_motion(store: Store, timestamp: float) -> None:
    """Count one unclassified motion event, keeping only the hour it fell in."""
    seen = store.get_meta(UNCLASSIFIED_MOTION_KEY) or {}
    hour = float(timestamp - timestamp % 3600)
    store.set_meta(
        UNCLASSIFIED_MOTION_KEY,
        {
            "count": int(seen.get("count", 0)) + 1,
            "last_hour": max(float(seen.get("last_hour") or 0.0), hour),
        },
    )


@dataclass
class HistoryBackfill:
    """What one pass over a camera's Event History found."""

    pages: int = 0
    rows: int = 0
    vehicle_added: int = 0
    vehicle_on_record: int = 0
    unclassified: int = 0
    outside_window: int = 0
    unreadable: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "by_type"}
        data["by_type"] = dict(self.by_type)
        return data


async def backfill_vehicle_history(
    store: Store,
    client: RingClient,
    camera_id: str,
    *,
    days: float = 7.0,
    now: float | None = None,
    max_pages: int = HISTORY_MAX_PAGES,
) -> HistoryBackfill:
    """Recover vehicle-motion events from the camera's Event History.

    This exists for cameras only. There is no sensor equivalent, which is why
    the first useful window model is weeks rather than hours after linking.
    What it buys is the street record across any time SASH was not listening.

    A row is ``{"id", "attributes": {"event_type", "start", "end"}}`` with
    ``start`` and ``end`` in epoch milliseconds. Neither the type filter nor a
    time range can be left to the server, so both are applied here, to every
    row of every page. Only ``motion.vehicle`` rows are stored; plain
    ``motion`` is counted as unclassified, and every other type is counted by
    name and dropped.
    """
    end = time.time() if now is None else now
    start = end - days * 86400.0
    report = HistoryBackfill()
    on_record = _OnRecord(
        store.vehicle_event_times(start - HISTORY_DEDUPE_S, end + HISTORY_DEDUPE_S)
    )
    page_key: str | None = None
    seen_keys: set[str] = set()
    while report.pages < max_pages:
        rows, next_key = await client.event_history(
            camera_id, event_types=ev.HISTORY_VEHICLE, page_key=page_key
        )
        report.pages += 1
        if not rows:
            break
        for row in rows:
            _apply_history_row(store, camera_id, row, start, end, on_record, report)
        if not next_key or next_key in seen_keys:
            break
        seen_keys.add(next_key)
        page_key = next_key
    return report


class _OnRecord:
    """Vehicle events stored before a backfill, each claimable by one history row.

    One-to-one, so two cars a few seconds apart that both arrived by webhook
    absorb their two history rows, and a burst Ring lists twice is not
    collapsed into one car.
    """

    def __init__(self, times: list[float]):
        self.times = sorted(times)
        self.claimed: set[int] = set()

    def claim(self, stamp: float) -> bool:
        lo = bisect.bisect_left(self.times, stamp - HISTORY_DEDUPE_S)
        hi = bisect.bisect_right(self.times, stamp + HISTORY_DEDUPE_S)
        free = [i for i in range(lo, hi) if i not in self.claimed]
        if not free:
            return False
        self.claimed.add(min(free, key=lambda i: abs(self.times[i] - stamp)))
        return True


def _apply_history_row(
    store: Store,
    camera_id: str,
    row: dict[str, Any],
    start: float,
    end: float,
    on_record: _OnRecord,
    report: HistoryBackfill,
) -> None:
    report.rows += 1
    attrs = row.get("attributes") or {}
    kind = str(attrs.get("event_type") or "")
    report.by_type[kind] = report.by_type.get(kind, 0) + 1
    if kind not in (ev.HISTORY_VEHICLE, ev.HISTORY_UNCLASSIFIED):
        return
    stamp = ev.epoch_seconds(attrs.get("start"))
    if stamp is None:
        report.unreadable += 1
        return
    if not start <= stamp <= end:
        report.outside_window += 1
        return
    if kind == ev.HISTORY_UNCLASSIFIED:
        report.unclassified += 1
        note_unclassified_motion(store, stamp)
        return
    if on_record.claim(stamp):
        report.vehicle_on_record += 1
        return
    row_id = row.get("id") or f"{camera_id}:{int(stamp * 1000)}"
    store.add_vehicle_event(f"history:{row_id}", camera_id, stamp, "history")
    report.vehicle_added += 1
