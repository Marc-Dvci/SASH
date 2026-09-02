"""Street activity, from vehicle-motion events.

Ring classifies motion as ``vehicle``, and SASH counts those events. It does not
count vehicles. One car crossing the detection zone slowly can raise two events;
one crossing the far lane can raise none; a camera whose motion zone stops at
the garden gate raises none at all. The number is a proxy for how busy the
street is, normalised against this camera's own history, and every label in the
product says so.

Two things are derived here:

* an **activity index**, expressed as this camera's own percentile, because an
  absolute event count means nothing across two different mountings; and
* a **weekly profile**, the median activity by hour and by weekday/weekend,
  which is what the planner uses to look ahead. It is a forecast of the street's
  habit, not of the street, and quiet hours are quiet with much more reliability
  than busy hours are busy.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from ..config import BURST_MERGE_SECONDS

#: Resolution of the weekly profile, in minutes.
PROFILE_SLOT_MIN = 30
SLOTS_PER_DAY = 24 * 60 // PROFILE_SLOT_MIN


@dataclass
class ActivityIndex:
    """Vehicle-motion activity for one household."""

    times: list[float] = field(default_factory=list)
    weekday_profile: np.ndarray = field(default_factory=lambda: np.zeros(SLOTS_PER_DAY))
    weekend_profile: np.ndarray = field(default_factory=lambda: np.zeros(SLOTS_PER_DAY))
    percentile_grid: np.ndarray = field(default_factory=lambda: np.array([]))
    #: How far a single day departs from this street's own habit, as the ratio
    #: of an observed slot to the median of that slot across the record. It is
    #: what a forecast built on a median is missing, and the planner's
    #: prediction interval is drawn from it.
    ratio_grid: np.ndarray = field(default_factory=lambda: np.array([]))
    n_events: int = 0
    n_bursts: int = 0

    # --- current and historical ------------------------------------------
    def rate(self, start: float, end: float) -> float:
        """Events per minute over ``[start, end)``."""
        if end <= start or not self.times:
            return 0.0
        lo = bisect.bisect_left(self.times, start)
        hi = bisect.bisect_left(self.times, end)
        return (hi - lo) / ((end - start) / 60.0)

    def percentile(self, rate: float) -> float:
        """Where a rate sits in this camera's own distribution, 0-100."""
        if self.percentile_grid.size == 0:
            return 50.0
        return float(
            100.0 * np.searchsorted(self.percentile_grid, rate) / self.percentile_grid.size
        )

    def band(self, rate: float) -> str:
        p = self.percentile(rate)
        if p >= 90:
            return "very high"
        if p >= 75:
            return "high"
        if p >= 25:
            return "typical"
        return "low"

    # --- looking ahead -----------------------------------------------------
    def expected(self, when: float) -> float:
        """Median activity this street usually shows at this time of week."""
        stamp = dt.datetime.fromtimestamp(when, tz=dt.timezone.utc)
        slot = (stamp.hour * 60 + stamp.minute) // PROFILE_SLOT_MIN
        profile = self.weekday_profile if stamp.weekday() < 5 else self.weekend_profile
        return float(profile[slot % SLOTS_PER_DAY])

    def sample_scale(self, rng: np.random.Generator, size: int) -> np.ndarray:
        """Draws of how busy a day turns out relative to this street's habit."""
        if self.ratio_grid.size < 20:
            return np.ones(size)
        return rng.choice(self.ratio_grid, size=size, replace=True)

    def spread(self, level: float = 0.90) -> tuple[float, float]:
        """The middle ``level`` of that day-to-day ratio, for the UI to state."""
        if self.ratio_grid.size < 20:
            return (1.0, 1.0)
        lo = float(np.percentile(self.ratio_grid, 100 * (1 - level) / 2))
        hi = float(np.percentile(self.ratio_grid, 100 * (1 + level) / 2))
        return (lo, hi)

    def as_dict(self) -> dict:
        return {
            "n_events": self.n_events,
            "n_bursts": self.n_bursts,
            "weekday_profile": [round(v, 3) for v in self.weekday_profile],
            "weekend_profile": [round(v, 3) for v in self.weekend_profile],
        }


def merge_bursts(times: list[float], gap_s: float = BURST_MERGE_SECONDS) -> int:
    """Count activity bursts: events closer than ``gap_s`` are one burst.

    Reported alongside the raw count, never instead of it, and never called a
    vehicle count.
    """
    if not times:
        return 0
    bursts = 1
    last = times[0]
    for t in times[1:]:
        if t - last > gap_s:
            bursts += 1
        last = t
    return bursts


def build(
    event_times: list[float],
    *,
    slot_min: int = PROFILE_SLOT_MIN,
    now: float | None = None,
    offline_windows: list[tuple[float, float]] | None = None,
) -> ActivityIndex:
    """Build the index from the stored vehicle-motion event times.

    ``offline_windows`` are excluded from the profile: a camera that was offline
    reported no motion, which is not the same as a street with no traffic.
    """
    index = ActivityIndex(times=sorted(event_times))
    index.n_events = len(index.times)
    index.n_bursts = merge_bursts(index.times)
    if not index.times:
        return index

    start = index.times[0]
    end = now if now is not None else index.times[-1]
    if end <= start:
        return index

    outages = offline_windows or []
    slot_s = slot_min * 60.0
    n_slots = int((end - start) // slot_s) + 1
    counts = np.zeros(n_slots)
    for t in index.times:
        counts[min(int((t - start) // slot_s), n_slots - 1)] += 1
    rates = counts / slot_min

    keep = np.ones(n_slots, dtype=bool)
    for i in range(n_slots):
        slot_start = start + i * slot_s
        slot_end = slot_start + slot_s
        if any(a < slot_end and b > slot_start for a, b in outages):
            keep[i] = False

    index.percentile_grid = np.sort(rates[keep]) if keep.any() else np.sort(rates)

    weekday_bins: list[list[float]] = [[] for _ in range(SLOTS_PER_DAY)]
    weekend_bins: list[list[float]] = [[] for _ in range(SLOTS_PER_DAY)]
    for i in range(n_slots):
        if not keep[i]:
            continue
        stamp = dt.datetime.fromtimestamp(start + i * slot_s, tz=dt.timezone.utc)
        slot = (stamp.hour * 60 + stamp.minute) // slot_min
        target = weekday_bins if stamp.weekday() < 5 else weekend_bins
        target[slot % SLOTS_PER_DAY].append(rates[i])

    index.weekday_profile = np.array(
        [float(np.median(v)) if v else 0.0 for v in weekday_bins]
    )
    index.weekend_profile = np.array(
        [float(np.median(v)) if v else 0.0 for v in weekend_bins]
    )
    index.ratio_grid = ratio_grid(weekday_bins, weekend_bins)
    return index


def ratio_grid(
    weekday_bins: list[list[float]], weekend_bins: list[list[float]]
) -> np.ndarray:
    """Every observed slot divided by the median of its own slot.

    Pooled across slots, because there is not enough of any single slot to
    describe its own spread, and because the thing being described -- a day that
    is busier or quieter than usual -- is a property of the day, not of the hour.
    """
    ratios: list[float] = []
    for bins in (weekday_bins, weekend_bins):
        for values in bins:
            if len(values) < 4:
                continue
            median = float(np.median(values))
            if median <= 1e-6:
                continue
            ratios.extend(v / median for v in values)
    return np.sort(np.array(ratios)) if ratios else np.array([])
