"""Turn Ring's three event streams into one table of observations.

The shape of this table is dictated by the API, not by preference. The Air
Quality Monitor reports roughly every fifteen minutes and the status endpoint
returns the last reported reading, so the finest honest unit of analysis is the
gap between two consecutive fresh PM2.5 readings. Everything else -- vehicle
motion events, contact transitions -- is aggregated onto that gap.

Two details decide whether the estimate is any good:

* **Sample lag.** A reading that represents the preceding quarter hour is not a
  reading of the instant it is stamped. SASH aggregates each interval's
  covariates over a window shifted back by half the reporting period. Ring's
  documentation does not state whether the reported value is instantaneous or an
  average, so this is a stated assumption with a knob on it, not a fact.

* **Offline is not zero.** A camera that is offline reports no vehicle motion.
  Treating that as a quiet street is the single easiest way to invent a result,
  so intervals overlapping a camera outage are marked and excluded from every
  street term.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from ..config import (
    AQM_MAX_SAMPLE_AGE_MIN,
    AQM_REPORT_INTERVAL_MIN,
    INTERVAL_MAX_MINUTES,
    INTERVAL_MIN_MINUTES,
)
from ..store import Store

#: Offset between the timestamp on a reading and the centre of the period it
#: describes. Ring's documentation does not state whether the reported value is
#: instantaneous or an average over the preceding cadence, and the two imply
#: alignments half an interval apart. SASH does not guess: ``calibrate_lag``
#: picks the alignment that best explains the home's own contact transitions,
#: and this is only the value used until it has enough of them.
SAMPLE_LAG_MIN = 0.0

#: Candidate alignments, in minutes, searched by ``calibrate_lag``.
LAG_CANDIDATES = (0.0, AQM_REPORT_INTERVAL_MIN / 4.0, AQM_REPORT_INTERVAL_MIN / 2.0)


@dataclass
class Interval:
    """One observation: the change in indoor PM2.5 between two readings."""

    t0: float
    t1: float
    dt_h: float
    c0: float
    c1: float
    activity: float  # vehicle-motion events per minute over the covariate window
    open_frac: dict[str, float] = field(default_factory=dict)
    temperature: float | None = None
    humidity: float | None = None
    co: float | None = None
    camera_online: bool = True
    aq_fresh: bool = True
    hour: float = 0.0
    weekday: bool = True
    episode_id: str | None = None
    indoor_source: bool = False
    source_rate: float = 0.0

    @property
    def rate(self) -> float:
        """Rate of change of indoor PM2.5, ug/m3 per hour."""
        return (self.c1 - self.c0) / self.dt_h

    @property
    def any_open(self) -> bool:
        return any(v > 0.02 for v in self.open_frac.values())

    @property
    def usable(self) -> bool:
        """Usable for the closed-envelope stage. Street terms need the camera."""
        return self.aq_fresh and self.dt_h > 0

    def open_openings(self) -> list[str]:
        return [k for k, v in self.open_frac.items() if v > 0.02]


def open_state_series(store: Store, opening_ids: list[str]) -> dict[str, list[tuple[float, str]]]:
    """Contact transitions per opening, sorted."""
    series: dict[str, list[tuple[float, str]]] = {oid: [] for oid in opening_ids}
    for device_id, at, state in store.contact_events():
        if device_id in series:
            series[device_id].append((at, state))
    for events in series.values():
        events.sort()
    return series


def open_fraction(
    transitions: list[tuple[float, str]], start: float, end: float
) -> float:
    """Fraction of ``[start, end)`` during which the opening was open.

    The state before the first recorded transition is taken as closed: SASH has
    no history before the account was linked and will not invent one.
    """
    if end <= start:
        return 0.0
    times = [t for t, _ in transitions]
    idx = bisect.bisect_right(times, start) - 1
    state = transitions[idx][1] if idx >= 0 else "closed"

    open_seconds = 0.0
    cursor = start
    i = bisect.bisect_right(times, start)
    while i < len(transitions) and transitions[i][0] < end:
        t, next_state = transitions[i]
        if state == "open":
            open_seconds += t - cursor
        cursor = t
        state = next_state
        i += 1
    if state == "open":
        open_seconds += end - cursor
    return max(0.0, min(1.0, open_seconds / (end - start)))


def _episode_ids(
    transitions: list[tuple[float, str]], opening_id: str
) -> list[tuple[float, float, str]]:
    """Contiguous open episodes, as ``(start, end, episode_id)``."""
    episodes: list[tuple[float, float, str]] = []
    start: float | None = None
    for i, (t, state) in enumerate(transitions):
        if state == "open" and start is None:
            start = t
        elif state == "closed" and start is not None:
            episodes.append((start, t, f"{opening_id}:{int(start)}"))
            start = None
    if start is not None:
        episodes.append((start, float("inf"), f"{opening_id}:{int(start)}"))
    return episodes


def build(
    store: Store,
    *,
    sample_lag_min: float = SAMPLE_LAG_MIN,
    activity_window_min: float | None = None,
) -> list[Interval]:
    """Build the observation table from everything currently in the store."""
    import datetime as _dt

    aq_devices = [d for d in store.devices(role="indoor_air")]
    if not aq_devices:
        return []
    aq_id = aq_devices[0].device_id
    samples = store.aq_samples(aq_id)
    if len(samples) < 2:
        return []

    openings = [d for d in store.devices(role="opening")]
    opening_ids = [d.device_id for d in openings]
    transitions = open_state_series(store, opening_ids)

    episodes: list[tuple[float, float, str]] = []
    for oid in opening_ids:
        episodes.extend(_episode_ids(transitions[oid], oid))
    episodes.sort()

    vehicle_times = store.vehicle_event_times()

    cameras = [d for d in store.devices(role="street_camera")]
    outages = _offline_windows(store, [c.device_id for c in cameras])

    lag_s = sample_lag_min * 60.0
    intervals: list[Interval] = []

    for prev, cur in zip(samples, samples[1:]):
        if prev["pm25"] is None or cur["pm25"] is None:
            continue
        dt_min = (cur["reported_at"] - prev["reported_at"]) / 60.0
        if not (INTERVAL_MIN_MINUTES <= dt_min <= INTERVAL_MAX_MINUTES):
            continue
        # A reading fetched long after it was reported cannot anchor an interval.
        if (cur["fetched_at"] - cur["reported_at"]) / 60.0 > AQM_MAX_SAMPLE_AGE_MIN:
            continue

        # Covariate window, shifted back by the assumed reporting lag.
        w0 = prev["reported_at"] - lag_s
        w1 = cur["reported_at"] - lag_s
        if activity_window_min:
            w0 = w1 - activity_window_min * 60.0

        frac = {oid: open_fraction(transitions[oid], w0, w1) for oid in opening_ids}

        lo = bisect.bisect_left(vehicle_times, w0)
        hi = bisect.bisect_left(vehicle_times, w1)
        minutes = max((w1 - w0) / 60.0, 1e-6)
        activity = (hi - lo) / minutes

        camera_online = not any(a < w1 and b > w0 for a, b in outages)

        episode_id = None
        for start, end, eid in episodes:
            if start < w1 and end > w0:
                episode_id = eid
                break

        stamp = _dt.datetime.fromtimestamp(prev["reported_at"], tz=_dt.timezone.utc)
        intervals.append(
            Interval(
                t0=prev["reported_at"],
                t1=cur["reported_at"],
                dt_h=dt_min / 60.0,
                c0=float(prev["pm25"]),
                c1=float(cur["pm25"]),
                activity=activity,
                open_frac=frac,
                temperature=cur.get("temperature"),
                humidity=cur.get("humidity"),
                co=cur.get("co"),
                camera_online=camera_online,
                aq_fresh=True,
                hour=stamp.hour + stamp.minute / 60.0,
                weekday=stamp.weekday() < 5,
                episode_id=episode_id,
            )
        )

    return intervals


def _offline_windows(store: Store, device_ids: list[str]) -> list[tuple[float, float]]:
    """Reconstruct offline windows from device_online / device_offline events."""
    windows: list[tuple[float, float]] = []
    for device_id in device_ids:
        down: float | None = None
        for at, online in store.health_transitions(device_id):
            if not online and down is None:
                down = at
            elif online and down is not None:
                windows.append((down, at))
                down = None
        if down is not None:
            windows.append((down, float("inf")))
    return windows
