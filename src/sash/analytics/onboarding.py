"""What SASH asks a new household to do, so the first weeks are a calibration.

A Ring sensor integration starts blind: there is no sensor history to backfill,
so every number SASH puts on a window has to be earned from airings that happen
after the household links its account. Left to its habit, a household earns
them slowly, and not because it airs too little. It airs through the same
window, at the same hours, while it cooks. The reporting gate in
:mod:`sash.analytics.estimator` needs the opposite on three counts:

* **Readings after cooking, with only this window open.** The air exchange rate
  is a carbon-monoxide decay, and the decay only exists while the air still
  carries what the hob made. An airing that starts after the cooking stops, with
  the other windows shut, is the tracer test a technician would have run.
* **Airings at both busy and quiet times of the street.** Street coupling is a
  slope against traffic, and a window only ever opened at the evening peak has
  no slope.
* **Separate airings through every window,** including the one nobody uses.

So instead of a spinner, SASH asks for those airings by name, one window at a
time, and shows how far each window has got. ``bench/onboarding.py`` runs the
same households twice, once on their habit and once following these steps, and
measures how many days each takes to reach a first plan, a number on every
window and a first called comparison.

Every step is an ordinary thirty-minute airing, right after the household
cooks or at an hour it already airs. None is published to the ledger: a
calibration airing is a measurement, not a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..config import MIN_INTERVALS_PER_OPENING
from .activity import ActivityIndex
from .estimator import (
    MIN_ACTIVITY_SPREAD,
    MIN_EPISODES,
    MIN_TRACER_ROWS,
    HomeModel,
)
from .intervals import Interval
from .sources import CO_SOURCE_PPM_PER_H

#: How long a calibration airing lasts, in minutes. Two Air Quality Monitor
#: readings, so one airing can contribute two decay measurements.
CALIBRATION_MIN = 30.0

#: Cooking is recognised from at least this many readings with a combustion
#: source running, before SASH names the household's usual cooking time.
MIN_COOKING_READINGS = 4


@dataclass
class Step:
    """One thing the household can do next, for one window."""

    opening_id: str
    opening_label: str
    kind: str  # after_cooking | busy_street | quiet_street | any
    text: str
    have: int
    need: int
    unit: str
    progress: float
    suggested_at: float | None = None
    duration_min: float = CALIBRATION_MIN

    def as_dict(self) -> dict:
        return {
            "opening_id": self.opening_id,
            "opening_label": self.opening_label,
            "kind": self.kind,
            "text": self.text,
            "have": self.have,
            "need": self.need,
            "unit": self.unit,
            "progress": round(self.progress, 3),
            "suggested_at": self.suggested_at,
            "duration_min": self.duration_min,
        }


def cooking_hours(intervals: Sequence[Interval]) -> list[float]:
    """The hours of the day, UTC, at which this household's hob is usually on.

    Read off the combustion source term that :mod:`sash.analytics.sources`
    already computes, so a kettle on an induction hob does not count and a gas
    hob does. Returns at most two peaks, earliest first.
    """
    hours = [
        iv.hour
        for iv in intervals
        if iv.source_rate is not None and iv.source_rate > CO_SOURCE_PPM_PER_H
    ]
    if len(hours) < MIN_COOKING_READINGS:
        return []
    counts = np.histogram(hours, bins=24, range=(0.0, 24.0))[0]
    peaks: list[float] = []
    for h in np.argsort(counts)[::-1]:
        if counts[h] < 2 or len(peaks) == 2:
            break
        if all(min(abs(h - p), 24 - abs(h - p)) >= 3 for p in peaks):
            peaks.append(float(h))
    return sorted(peaks)


def _clock(hour: float) -> str:
    h = int(hour) % 24
    return f"{h:02d}:{int(round((hour % 1) * 60)) % 60:02d}"


def _street_time(
    activity: ActivityIndex,
    airing_hours: np.ndarray,
    now: float,
    *,
    busy: bool,
) -> float | None:
    """The busiest, or quietest, quarter hour in the next day that this home airs at."""
    from .planner import START_STEP_MIN, within_habit

    best: tuple[float, float] | None = None
    for k in range(int(24 * 60 // START_STEP_MIN)):
        t = now + k * START_STEP_MIN * 60.0
        if not within_habit((t % 86400.0) / 3600.0, airing_hours):
            continue
        rate = activity.expected(t)
        key = -rate if busy else rate
        if best is None or key < best[0]:
            best = (key, t)
    return None if best is None else best[1]


def steps(
    model: HomeModel,
    intervals: Sequence[Interval],
    activity: ActivityIndex,
    now: float,
) -> list[Step]:
    """One step per window that SASH cannot yet put a number on, nearest first.

    The step names the requirement that is furthest from met, because that is
    the one holding the window back, and says how much of it is done.
    """
    from .planner import airing_hours

    if not model.openings:
        return []
    cook = cooking_hours(intervals)
    cook_text = (
        "usually around " + " and ".join(_clock(h) for h in cook)
        if cook
        else "whenever you next cook"
    )
    hours = airing_hours(intervals)
    street_median = float(np.median([iv.activity for iv in intervals])) if intervals else 0.0

    out: list[Step] = []
    for est in model.openings.values():
        if est.reported:
            continue
        label = est.label
        others = "with the other windows shut"
        # Each requirement as (have, need), in the gate's own units.
        tracer = (est.n_tracer, MIN_TRACER_ROWS)
        episodes = (est.n_episodes, MIN_EPISODES)
        readings = (est.n_intervals, MIN_INTERVALS_PER_OPENING)
        shortfalls = [
            (min(1.0, h / n), name, h, n)
            for name, (h, n) in (
                ("tracer", tracer),
                ("episodes", episodes),
                ("readings", readings),
            )
        ]
        progress, name, have, need = min(shortfalls, key=lambda s: s[0])

        if progress < 1.0 and name == "tracer":
            out.append(
                Step(
                    est.opening_id,
                    label,
                    "after_cooking",
                    f"After you cook ({cook_text}), open the {label} for "
                    f"{CALIBRATION_MIN:.0f} minutes {others}.",
                    have,
                    need,
                    "readings after cooking",
                    progress,
                )
            )
            continue
        if progress < 1.0:
            out.append(
                Step(
                    est.opening_id,
                    label,
                    "any",
                    f"Air through the {label} {others}, at any time you would "
                    "normally air. SASH has seen too few airings through it to "
                    "measure it.",
                    have,
                    need,
                    "airings" if name == "episodes" else "readings with only this window open",
                    progress,
                )
            )
            continue

        # Enough airings; what is missing is a spread of street conditions.
        if model.fitted and est.n_intervals and est.activity_spread < MIN_ACTIVITY_SPREAD:
            seen = [
                iv.activity
                for iv in intervals
                if iv.open_frac.get(est.opening_id, 0.0) > 0.5
            ]
            busy = bool(seen) and float(np.median(seen)) < street_median
            at = _street_time(activity, hours, now, busy=busy)
            when = (
                f"around {_clock((at % 86400.0) / 3600.0)}"
                if at is not None
                else ("when the street is busy" if busy else "when the street is quiet")
            )
            out.append(
                Step(
                    est.opening_id,
                    label,
                    "busy_street" if busy else "quiet_street",
                    f"Air through the {label} {when}, when the street is "
                    f"{'busier' if busy else 'quieter'} than it has been on every "
                    f"airing through it so far. Street pollution is measured as a "
                    "difference between busy and quiet, and this window has only "
                    "seen one.",
                    int(round(100 * est.activity_spread / MIN_ACTIVITY_SPREAD)),
                    100,
                    "per cent of the street range",
                    est.activity_spread / MIN_ACTIVITY_SPREAD,
                    suggested_at=at,
                )
            )
            continue
        out.append(
            Step(
                est.opening_id,
                label,
                "any",
                f"Air through the {label} {others}, at a different indoor level "
                "from before: once soon after cooking, once on a quiet day.",
                0,
                1,
                "",
                0.9,
            )
        )

    out.sort(key=lambda s: s.progress)
    return out


def describe(model: HomeModel, step_list: Sequence[Step]) -> str:
    """One sentence on where the household stands, for the card's subtitle."""
    total = len(model.openings)
    measured = sum(1 for o in model.openings.values() if o.reported)
    if not step_list:
        return f"Every window is measured ({measured} of {total})."
    return (
        f"{measured} of {total} windows measured. Cooking fumes fading through a "
        "window are how SASH measures the air it moves, so the quickest way to "
        "the rest is half an hour through each, right after you cook or at a "
        "time you already air."
    )
