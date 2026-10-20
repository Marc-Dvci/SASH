"""The decision, and the check on the decision.

A dashboard that tells a household its street is busy has told it something it
could see from the window. The output of SASH is a plan: *which* opening, *when*,
for *how long*, and what that will cost in particle load compared with the
alternatives.

The cost of a plan is the area between two forecasts of the same room: what the
indoor PM2.5 does with the opening open, and what it would have done with
everything shut. It is expressed in ug/m3 x minutes, which is exposure, not
concentration -- twenty minutes at ten over baseline and ten minutes at twenty
over baseline cost the same.

Two design commitments.

**The target comes from the household, not from a guideline.** SASH aims to
deliver the amount of air exchange this home has historically chosen for itself,
and it opens the same total amount of window. It moves *when* and *which*, never
*how much*, so it cannot quietly trade the household's air for a cleaner number.

**Every plan is scored afterwards.** When the opening actually opens and closes,
the realised intake is computed from the readings that arrived and compared with
the interval the plan was published with. The ledger of those comparisons is
part of the product: a recommendation is worth what its track record says.

**Street particulate is not the only reason to open a window, and when it is
not the main one SASH steps aside.** Four rules come before the search, in
this order, and each is stated on the card when it fires:

1. Carbon monoxide at or above Ring's own 9 ppm alert: no plan at all. That is
   a safety event, not a scheduling question.
2. Something in the flat is fouling the air now (cooking, a shower, a candle,
   recognised by :mod:`sash.analytics.sources`), carbon monoxide is above its
   usual level, or humidity is at 70 % or more: the airing starts within half an
   hour. SASH still picks the window, never the time.
3. The home has had less than half its usual day's air in the last
   twenty-four hours: the search looks three hours ahead, not twelve. A full
   day, because every home goes without air overnight and a shorter window
   would call each morning overdue.
4. Otherwise, the full twelve-hour search.

The forecast of street activity is this street's own weekly habit, not a
prediction of the street. A road closure or a delivery surge will not be in it.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..config import CO_ALERT_PPM
from . import onboarding
from .activity import ActivityIndex
from .estimator import CO_OUTDOOR_PPM, CO_TRACER_FLOOR_PPM, HomeModel, integrate
from .intervals import Interval

#: How far ahead the planner looks, in hours.
HORIZON_H = 12.0

#: Granularity of candidate start times, in minutes.
START_STEP_MIN = 15

#: The room is followed this long after the opening closes, because the load
#: that came in is still in the room.
RECOVERY_MIN = 90.0

#: An opening counts as shut, for the purpose of measuring another opening, below
#: this open fraction. A contact sensor that flickers for one reading of fifteen
#: minutes should not void an hour of measurement.
OTHER_OPENING_TOL = 0.05

#: Bounds on a recommended airing, in minutes.
MIN_DURATION_MIN = 10.0
MAX_DURATION_MIN = 90.0

#: Fallback air-exchange target, in air changes, used only until the household
#: has enough history for its own median.
FALLBACK_TARGET_EXCHANGES = 1.2

#: A start time is only offered if this household has itself opened something
#: near that time of day at least this many times. Nobody opens a window at four
#: in the morning because the street is quiet then, and a plan a household will
#: not act on is not a plan.
MIN_HABIT_SUPPORT = 3

#: How near, in minutes, an observed airing has to be to count as support.
HABIT_WINDOW_MIN = 45.0

#: Two plans whose forecasts are within this fraction of each other are not
#: meaningfully different, and SASH takes the earlier one. A household will not
#: hold its breath until the evening to save two per cent.
TIE_FRACTION = 0.08

#: Interval multiplier used before a household has enough scored airings to
#: calibrate its own. Deliberately wide: an uncalibrated interval that is too
#: narrow is a promise SASH has no basis for.
UNCALIBRATED_K = 4.0

#: Smallest interval shape, in ug/m3 x minutes.
INTERVAL_FLOOR_ABS = 1.0

#: When the reason to air is inside the flat, the airing starts within this
#: many minutes. Without the rule, right after a meal the cheapest plan is
#: usually "now" anyway, because a dirty room is cleaned by opening it -- but
#: not always: on benchmark homes with 60 days of history, 2 of 96 post-meal
#: plans said to wait more than two hours, one of them eleven. The rule makes
#: "now" a guarantee rather than an outcome.
CLEAR_WITHIN_MIN = 30.0

#: An indoor emission counts as current if it was running in a reading that
#: ended at most this long ago.
INDOOR_RECENT_MIN = 45.0

#: Carbon monoxide above this, ppm, is a reason to air now. Outdoor CO on a
#: residential street sits near 0.05 ppm and a sealed flat returns to it within
#: hours, so a reading this high means something is still burning or has just
#: stopped.
CO_CLEAR_PPM = 1.0

#: Relative humidity at which airing stops being optional, %. Sustained indoor
#: humidity at this level is where condensation and mould on cold walls begin.
HUMIDITY_CLEAR_PCT = 70.0

#: How far ahead SASH looks when the home is overdue, in hours.
OVERDUE_WITHIN_H = 3.0


def interval_scale(c_star: float, c0: float, duration_min: float) -> float:
    """The natural size of one airing's cost, in ug/m3 x minutes.

    An interval has to be scaled by something, and the parameter bootstrap is
    the wrong thing. It knows how uncertain the exchange rate and the street
    coupling are; it does not know that next Tuesday's regional air will be
    dirtier than this Tuesday's, and on a fifteen-minute cadence that is the
    larger term by some way. What an airing's cost actually scales with is the
    concentration in play -- the level the home settles at, plus however far
    from it the room happens to be right now -- multiplied by how long the
    opening is open. :func:`sash.analytics.holdout.calibrate` turns that shape
    into a width, using this household's own past airings.
    """
    return max((abs(c_star) + abs(c0 - c_star)) * duration_min, INTERVAL_FLOOR_ABS)


@dataclass
class Demand:
    """Why the home would want airing now."""

    target_exchanges: float
    delivered_12h: float
    target_day: float = 0.0
    delivered_24h: float = 0.0
    drivers: list[str] = field(default_factory=list)
    humidity: float | None = None
    co: float | None = None
    level: str = "none"  # none | due | overdue

    def as_dict(self) -> dict:
        return {
            "target_exchanges": round(self.target_exchanges, 2),
            "delivered_12h": round(self.delivered_12h, 2),
            "target_day": round(self.target_day, 2),
            "delivered_24h": round(self.delivered_24h, 2),
            "drivers": self.drivers,
            "humidity": self.humidity,
            "co": self.co,
            "level": self.level,
        }


@dataclass
class Plan:
    plan_id: str
    opening_id: str
    opening_label: str
    start_at: float
    duration_min: float
    pred_exchanges: float
    pred_intake: float
    pred_intake_lo: float
    pred_intake_hi: float
    expected_activity: float
    activity_band: str
    exploration: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "opening_id": self.opening_id,
            "opening_label": self.opening_label,
            "start_at": self.start_at,
            "duration_min": round(self.duration_min, 1),
            "pred_exchanges": round(self.pred_exchanges, 2),
            "pred_intake": round(self.pred_intake, 1),
            "pred_intake_lo": round(self.pred_intake_lo, 1),
            "pred_intake_hi": round(self.pred_intake_hi, 1),
            "expected_activity": round(self.expected_activity, 3),
            "activity_band": self.activity_band,
            "exploration": self.exploration,
            "note": self.note,
        }


@dataclass
class Recommendation:
    best: Plan | None
    alternatives: list[Plan]
    habit: Plan | None
    demand: Demand
    saving_pct: float | None
    reason: str
    saving_lo: float | None = None
    saving_hi: float | None = None
    p_better: float | None = None
    n_draws: int = 0
    n_considered: int = 0
    n_airings_observed: int = 0
    habit_hours: float = 24.0
    mode: str = "plan"  # plan | overdue | clear_now | safety
    mode_reason: str = ""
    calibration: list[onboarding.Step] = field(default_factory=list)
    calibration_summary: str = ""

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_reason": self.mode_reason,
            # Not "calibration": the service puts the interval calibration there.
            "onboarding": [s.as_dict() for s in self.calibration],
            "onboarding_summary": self.calibration_summary,
            "best": self.best.as_dict() if self.best else None,
            "alternatives": [p.as_dict() for p in self.alternatives],
            "habit": self.habit.as_dict() if self.habit else None,
            "demand": self.demand.as_dict(),
            "saving_pct": None if self.saving_pct is None else round(self.saving_pct, 1),
            "saving_lo": None if self.saving_lo is None else round(self.saving_lo, 1),
            "saving_hi": None if self.saving_hi is None else round(self.saving_hi, 1),
            "p_better": None if self.p_better is None else round(self.p_better, 3),
            "n_draws": self.n_draws,
            "reason": self.reason,
            "n_considered": self.n_considered,
            "n_airings_observed": self.n_airings_observed,
            "habit_hours": round(self.habit_hours, 1),
        }


# --- demand -----------------------------------------------------------------


def air_changes_delivered(
    intervals: Sequence[Interval], model: HomeModel, since: float, until: float
) -> float:
    """Air changes delivered through monitored openings in a period."""
    total = 0.0
    for iv in intervals:
        if iv.t1 <= since or iv.t0 >= until:
            continue
        overlap_h = (min(iv.t1, until) - max(iv.t0, since)) / 3600.0
        for oid, frac in iv.open_frac.items():
            est = model.openings.get(oid)
            if est and frac > 0:
                total += est.exchange * frac * overlap_h
    return total


def demand(
    intervals: Sequence[Interval],
    model: HomeModel,
    now: float,
    *,
    humidity: float | None = None,
    co: float | None = None,
) -> Demand:
    """How much airing the home is short of, against its own habit."""
    daily = []
    if intervals:
        first = intervals[0].t0
        day = first
        while day + 86400.0 <= now:
            daily.append(air_changes_delivered(intervals, model, day, day + 86400.0))
            day += 86400.0
    target_day = float(np.median(daily)) if len(daily) >= 5 else FALLBACK_TARGET_EXCHANGES * 2
    target_12h = max(target_day / 2.0, 0.3)

    delivered = air_changes_delivered(intervals, model, now - 12 * 3600.0, now)
    delivered_day = air_changes_delivered(intervals, model, now - 24 * 3600.0, now)
    drivers: list[str] = []
    if delivered < target_12h:
        drivers.append(
            f"{delivered:.1f} of the {target_12h:.1f} air changes this home usually "
            "gets in half a day"
        )

    if humidity is not None:
        recent = [iv.humidity for iv in intervals[-800:] if iv.humidity is not None]
        if len(recent) >= 50:
            high = float(np.percentile(recent, 75))
            # Two decimals apart is not a reason to open a window, and rounding
            # a one-point gap to whole per cent prints the same number twice.
            if humidity > high + 1.5:
                drivers.append(
                    f"humidity {humidity:.0f}% is above the {high:.0f}% this home "
                    "sits below three quarters of the time"
                )
    if co is not None and co > CO_CLEAR_PPM:
        drivers.append(f"CO {co:.1f} ppm is above its usual level")

    level = "none"
    # Judged over a whole day: a twelve-hour window ending in the morning is
    # empty in every home that sleeps with its windows shut.
    if delivered_day < max(target_day, 0.6) * 0.5:
        level = "overdue"
    elif drivers:
        level = "due"

    return Demand(
        target_exchanges=target_12h,
        delivered_12h=delivered,
        target_day=max(target_day, 0.6),
        delivered_24h=delivered_day,
        drivers=drivers,
        humidity=humidity,
        co=co,
        level=level,
    )


def _carries_tracer(co: float | None) -> bool:
    """True when the CO channel is high enough to start a usable decay."""
    return co is not None and co - CO_OUTDOOR_PPM > CO_TRACER_FLOOR_PPM


def step_aside(
    intervals: Sequence[Interval],
    need: Demand,
    now: float,
    *,
    humidity: float | None = None,
    co: float | None = None,
) -> tuple[str, str, float]:
    """Which rule governs this decision: ``(mode, reason, look-ahead hours)``.

    Minimising street particulate is the right objective only while nothing
    more pressing is going on. The rules are ordered by how pressing, and the
    first that fires wins. The reason is a sentence the card shows as it is.
    """
    if co is not None and co >= CO_ALERT_PPM:
        return (
            "safety",
            f"Carbon monoxide is at {co:.1f} ppm, at or above the {CO_ALERT_PPM:.0f} ppm "
            "at which Ring's monitor alerts. Open the windows now and check anything "
            "that burns fuel. SASH makes no plan while it is this high.",
            0.0,
        )

    why: list[str] = []
    recent = [iv for iv in intervals if iv.t1 >= now - INDOOR_RECENT_MIN * 60.0]
    if any(iv.indoor_source for iv in recent):
        why.append(
            "the air still carries cooking fumes"
            if _carries_tracer(co)
            else "something inside the flat has just been adding to the air"
        )
    if co is not None and co > CO_CLEAR_PPM:
        why.append(f"carbon monoxide is at {co:.1f} ppm, above its usual level")
    if humidity is not None and humidity >= HUMIDITY_CLEAR_PCT:
        why.append(f"humidity is at {humidity:.0f}%")
    if why:
        return (
            "clear_now",
            "Air now: " + "; ".join(why) + ". When the reason to air is inside the "
            "flat, SASH picks the window but not the time.",
            CLEAR_WITHIN_MIN / 60.0,
        )

    if need.level == "overdue":
        return (
            "overdue",
            f"This home has had {need.delivered_24h:.1f} of its usual "
            f"{need.target_day:.1f} air changes in the last day, so SASH looks "
            f"{OVERDUE_WITHIN_H:.0f} hours ahead rather than twelve.",
            OVERDUE_WITHIN_H,
        )
    return "plan", "", HORIZON_H


# --- when this household actually airs ---------------------------------------


def airing_hours(intervals: Sequence[Interval]) -> np.ndarray:
    """Hours of the day, UTC, at which this household has opened something.

    Recovered from the episode ids, each of which carries the epoch second the
    opening opened. One entry per airing, not per reading, so a long airing does
    not outvote a short one.
    """
    starts: set[float] = set()
    for iv in intervals:
        if not iv.episode_id or ":" not in iv.episode_id:
            continue
        try:
            starts.add(float(iv.episode_id.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return np.sort(np.array([(t % 86400.0) / 3600.0 for t in sorted(starts)]))


def within_habit(
    hour: float,
    hours: np.ndarray,
    *,
    window_min: float = HABIT_WINDOW_MIN,
    support: int = MIN_HABIT_SUPPORT,
) -> bool:
    """True when this household has aired near ``hour`` often enough to be offered it."""
    if hours.size == 0:
        return True
    delta = np.abs(hours - hour)
    delta = np.minimum(delta, 24.0 - delta)
    return int(np.count_nonzero(delta <= window_min / 60.0)) >= support


def habit_slots(hours: np.ndarray, *, step_min: int = 30) -> int:
    """How many half-hour slots of the day this household airs in."""
    if hours.size == 0:
        return 48
    n = 24 * 60 // step_min
    return sum(
        1 for i in range(n) if within_habit((i * step_min) / 60.0, hours)
    )


# --- forecasting a plan -----------------------------------------------------


def _forecast_intake(
    model: HomeModel,
    activity: ActivityIndex,
    start_c: float,
    c_star: float,
    now: float,
    opening_id: str,
    start_at: float,
    duration_min: float,
    *,
    theta: float | None = None,
    coupling: float | None = None,
    activity_scale: float = 1.0,
    step_min: float = 5.0,
) -> tuple[float, float]:
    """Return ``(intake, air_changes)`` for one candidate plan.

    ``theta``, ``coupling`` and ``activity_scale`` override the fitted values,
    which is how the prediction interval is produced: the same integration is
    run over draws of the two parameters and of how busy the day turns out
    relative to this street's habit.
    """
    est = model.openings.get(opening_id)
    if est is None:
        return 0.0, 0.0
    theta = est.exchange if theta is None else theta
    coupling = est.coupling if coupling is None else coupling

    end = start_at + (duration_min + RECOVERY_MIN) * 60.0
    steps_open: list[tuple[float, dict[str, float], float, float]] = []
    steps_shut: list[tuple[float, dict[str, float], float, float]] = []
    t = now
    dt_h = step_min / 60.0
    while t < end:
        a = activity.expected(t) * activity_scale
        frac = 1.0 if start_at <= t < start_at + duration_min * 60.0 else 0.0
        steps_open.append((dt_h, {opening_id: frac}, a, c_star))
        steps_shut.append((dt_h, {}, a, c_star))
        t += step_min * 60.0

    # Integrate with the override in place.
    saved = (est.exchange, est.coupling)
    est.exchange, est.coupling = theta, coupling
    try:
        opened = integrate(model, start_c, steps_open)
        shut = integrate(model, start_c, steps_shut)
    finally:
        est.exchange, est.coupling = saved

    intake = float(np.sum((np.array(opened) - np.array(shut))[1:]) * step_min)
    air_changes = theta * duration_min / 60.0
    return intake, air_changes


def recommend(
    model: HomeModel,
    activity: ActivityIndex,
    intervals: Sequence[Interval],
    *,
    now: float,
    current_pm: float,
    humidity: float | None = None,
    co: float | None = None,
    horizon_h: float = HORIZON_H,
    draws: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
    explore: bool = True,
    interval_k: float = UNCALIBRATED_K,
    seed: int = 3,
    rules: bool = True,
) -> Recommendation:
    """Rank every (opening, start time) pair over the horizon.

    ``explore`` lets a calibration airing take the place of the plan, and
    ``rules=False`` switches off :func:`step_aside` so the benchmark can measure
    what the rules cost.
    """
    need = demand(intervals, model, now, humidity=humidity, co=co)
    if rules:
        mode, mode_reason, rule_h = step_aside(
            intervals, need, now, humidity=humidity, co=co
        )
        horizon_h = min(horizon_h, rule_h)
    else:
        mode, mode_reason = "plan", ""
    calibration = onboarding.steps(model, intervals, activity, now)
    calibration_summary = onboarding.describe(model, calibration)

    def bare(reason: str, best: Plan | None = None) -> Recommendation:
        return Recommendation(
            best,
            [],
            None,
            need,
            None,
            reason,
            mode=mode,
            mode_reason=mode_reason,
            calibration=calibration,
            calibration_summary=calibration_summary,
        )

    if mode == "safety":
        return bare(mode_reason)

    # Right after cooking is the one moment an airing measures a window's air
    # exchange, because the fumes are the tracer. If a window still needs that
    # measurement and the CO is there to follow, this is the airing to ask for.
    # A candle or a shower calls for air too, but leaves no tracer.
    tracer_step = next((s for s in calibration if s.kind == "after_cooking"), None)
    calibrating = (
        _calibration_plan(tracer_step, now, activity)
        if explore
        and mode == "clear_now"
        and tracer_step is not None
        and _carries_tracer(co)
        else None
    )

    usable = [o for o in model.openings.values() if o.reported and o.exchange > 1e-3]
    if not usable:
        if calibrating is not None:
            return bare(mode_reason, calibrating)
        if mode == "clear_now":
            return bare(
                mode_reason + " Any window will do: SASH cannot rank them yet."
            )
        return bare(
            "SASH has not yet measured any opening well enough to rank them. "
            "The airings below are the quickest way there."
        )

    c_star = model.baseline.recent_floor
    rng = np.random.default_rng(seed)
    candidates: list[Plan] = []

    # SASH only ever offers a time this household has itself chosen before. The
    # quietest street of the week is at four in the morning and no plan that
    # starts there will ever be acted on.
    hours = airing_hours(intervals)
    offered_hours = habit_slots(hours) / 2.0

    for est in usable:
        duration = float(
            np.clip(
                60.0 * need.target_exchanges / est.exchange,
                MIN_DURATION_MIN,
                MAX_DURATION_MIN,
            )
        )
        steps = int(horizon_h * 60 // START_STEP_MIN)
        for k in range(steps):
            start_at = now + k * START_STEP_MIN * 60.0
            # An airing the flat needs now is not held to the household's usual
            # hours: people cook at unusual hours too.
            if mode != "clear_now" and not within_habit(
                (start_at % 86400.0) / 3600.0, hours
            ):
                continue
            intake, exchanges = _forecast_intake(
                model, activity, current_pm, c_star, now, est.opening_id, start_at, duration
            )
            half = interval_k * interval_scale(c_star, current_pm, duration)
            lo, hi = intake - half, intake + half
            expected_activity = activity.expected(start_at)
            candidates.append(
                Plan(
                    plan_id=uuid.uuid4().hex[:12],
                    opening_id=est.opening_id,
                    opening_label=est.label,
                    start_at=start_at,
                    duration_min=duration,
                    pred_exchanges=exchanges,
                    pred_intake=intake,
                    pred_intake_lo=lo,
                    pred_intake_hi=hi,
                    expected_activity=expected_activity,
                    activity_band=activity.band(expected_activity),
                )
            )

    if not candidates:
        rec = bare(
            f"Nothing in the next {horizon_h:.0f} hours falls in the hours this "
            "household actually airs. SASH will have a plan by then."
        )
        rec.n_airings_observed = int(hours.size)
        rec.habit_hours = offered_hours
        return rec

    candidates.sort(key=lambda p: p.pred_intake)
    cheapest = candidates[0]
    # Among the plans that are not meaningfully worse than the cheapest, take
    # the soonest. Ranking a whole day of quarter hours on a forecast produces
    # ties all the time, and breaking them by time is the only tie-break a
    # household would recognise.
    tolerance = TIE_FRACTION * max(abs(cheapest.pred_intake), 1.0)
    tied = [p for p in candidates if p.pred_intake <= cheapest.pred_intake + tolerance]
    best = min(tied, key=lambda p: p.start_at)

    # One alternative per other opening, at its own best time, plus the best
    # plan that starts within the next hour if the winner is later.
    alternatives: list[Plan] = []
    seen = {best.opening_id}
    for plan in candidates:
        if plan.opening_id not in seen:
            alternatives.append(plan)
            seen.add(plan.opening_id)
    soon = [p for p in candidates if p.start_at <= now + 3600.0]
    if soon and soon[0].plan_id != best.plan_id:
        alternatives.insert(0, soon[0])

    habit = _habit_plan(
        model, activity, intervals, now, current_pm, c_star, need.target_exchanges
    )
    saving = None
    if habit and habit.pred_intake > 1e-6:
        saving = 100.0 * (habit.pred_intake - best.pred_intake) / habit.pred_intake

    reason = _explain(best, habit, need, activity, mode_reason)

    if calibrating is not None:
        # The measurement takes the place of the plan, and says so. The plan it
        # replaced stays in the alternatives.
        alternatives.insert(0, best)
        best, saving, reason = calibrating, None, mode_reason

    comparison = (
        (None, None, None, 0)
        if best.exploration
        else _compare_plans(model, activity, best, habit, now, current_pm, c_star, draws, rng)
    )

    return Recommendation(
        best,
        alternatives[:3],
        habit,
        need,
        saving,
        reason,
        saving_lo=comparison[0],
        saving_hi=comparison[1],
        p_better=comparison[2],
        n_draws=comparison[3],
        n_considered=len(candidates),
        n_airings_observed=int(hours.size),
        habit_hours=offered_hours,
        mode=mode,
        mode_reason=mode_reason,
        calibration=calibration,
        calibration_summary=calibration_summary,
    )


#: Paired draws used to compare the recommendation against the habit.
N_COMPARE_DRAWS = 120


def _compare_plans(
    model: HomeModel,
    activity: ActivityIndex,
    best: Plan,
    habit: Plan | None,
    now: float,
    current_pm: float,
    c_star: float,
    draws: dict[str, tuple[np.ndarray, np.ndarray]] | None,
    rng: np.random.Generator,
) -> tuple[float | None, float | None, float | None, int]:
    """How sure is SASH that its plan beats the habit? Answered in pairs.

    The absolute cost of a single airing is hard to forecast, because most of
    the error is the weather: next Tuesday's regional air is not in any of this.
    The *difference* between two plans for the same home is much better
    determined, because both forecasts carry the same weather, the same starting
    concentration and the same day.

    So the two plans are forecast together, draw by draw. Each draw takes one
    joint bootstrap sample -- which already holds every opening's parameters as
    they were fitted alongside each other -- and one draw of how busy the day
    turns out, and applies both to both plans. What comes out is the fraction of
    draws in which the recommendation actually costs less than what this
    household would have done anyway.
    """
    if habit is None or habit.opening_id == best.opening_id or not draws:
        return None, None, None, 0
    a = draws.get(best.opening_id)
    b = draws.get(habit.opening_id)
    if a is None or b is None or a[0].size < 20 or b[0].size < 20:
        return None, None, None, 0

    size = min(a[0].size, b[0].size)
    pick = rng.choice(size, size=min(N_COMPARE_DRAWS, size), replace=size < N_COMPARE_DRAWS)
    scales = activity.sample_scale(rng, len(pick))

    savings: list[float] = []
    wins = 0
    for i, scale in zip(pick, scales):
        best_intake, _ = _forecast_intake(
            model,
            activity,
            current_pm,
            c_star,
            now,
            best.opening_id,
            best.start_at,
            best.duration_min,
            theta=float(a[0][i]),
            coupling=float(a[1][i]),
            activity_scale=float(scale),
        )
        habit_intake, _ = _forecast_intake(
            model,
            activity,
            current_pm,
            c_star,
            now,
            habit.opening_id,
            habit.start_at,
            habit.duration_min,
            theta=float(b[0][i]),
            coupling=float(b[1][i]),
            activity_scale=float(scale),
        )
        if best_intake < habit_intake:
            wins += 1
        if habit_intake > 1e-6:
            savings.append(100.0 * (habit_intake - best_intake) / habit_intake)

    if not savings:
        return None, None, wins / len(pick), len(pick)
    return (
        float(np.percentile(savings, 5)),
        float(np.percentile(savings, 95)),
        wins / len(pick),
        len(pick),
    )


def _calibration_plan(
    step: onboarding.Step, now: float, activity: ActivityIndex
) -> Plan:
    """A calibration airing, shaped as a plan so the card can show it.

    Habit is what makes this problem hard: a household that only ever opens one
    window gives SASH nothing to compare it against. Sending an airing through
    the other window is how the comparison gets made, so the recommendation is
    also the experiment. It is never silent about it, it carries no forecast,
    because the window it measures has none yet, and it is not published to the
    ledger.
    """
    expected = activity.expected(now)
    return Plan(
        plan_id=f"calibration-{uuid.uuid4().hex[:8]}",
        opening_id=step.opening_id,
        opening_label=step.opening_label,
        start_at=now,
        duration_min=step.duration_min,
        pred_exchanges=0.0,
        pred_intake=0.0,
        pred_intake_lo=0.0,
        pred_intake_hi=0.0,
        expected_activity=expected,
        activity_band=activity.band(expected),
        exploration=True,
        note=step.text,
    )


def _habit_plan(
    model: HomeModel,
    activity: ActivityIndex,
    intervals: Sequence[Interval],
    now: float,
    current_pm: float,
    c_star: float,
    target_exchanges: float,
) -> Plan | None:
    """What this household would have done: its usual opening, at its usual hour.

    Held to the same air budget as the recommendation, so the two forecasts
    differ only in which opening and which hour. A comparison that let one side
    move less air would be measuring the budget, not the decision.
    """
    counts: dict[str, int] = {}
    hours: dict[str, list[float]] = {}
    for iv in intervals:
        for oid, frac in iv.open_frac.items():
            if frac > 0.5:
                counts[oid] = counts.get(oid, 0) + 1
                hours.setdefault(oid, []).append(iv.hour)
    if not counts:
        return None
    oid = max(counts, key=lambda k: counts[k])
    est = model.openings.get(oid)
    if est is None or est.exchange <= 1e-3:
        return None

    # The next time this home would have aired through this opening, not the
    # median of its airing hours: a household that airs at eight and at seven in
    # the evening has a median of early afternoon, which is a time it never uses.
    own_hours = airing_hours([iv for iv in intervals if iv.open_frac.get(oid, 0.0) > 0.5])
    start = None
    for k in range(int(HORIZON_H * 60 // START_STEP_MIN) + 1):
        candidate = now + k * START_STEP_MIN * 60.0
        if within_habit((candidate % 86400.0) / 3600.0, own_hours):
            start = candidate
            break
    if start is None:
        usual_hour = float(np.median(hours[oid]))
        stamp = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
        start = stamp.replace(
            hour=int(usual_hour) % 24,
            minute=int((usual_hour % 1) * 60),
            second=0,
            microsecond=0,
        ).timestamp()
        if start < now:
            start += 86400.0

    duration = float(
        np.clip(60.0 * target_exchanges / est.exchange, MIN_DURATION_MIN, MAX_DURATION_MIN)
    )
    intake, exchanges = _forecast_intake(
        model, activity, current_pm, c_star, now, oid, start, duration
    )
    expected_activity = activity.expected(start)
    return Plan(
        plan_id="habit",
        opening_id=oid,
        opening_label=est.label,
        start_at=start,
        duration_min=duration,
        pred_exchanges=exchanges,
        pred_intake=intake,
        pred_intake_lo=intake,
        pred_intake_hi=intake,
        expected_activity=expected_activity,
        activity_band=activity.band(expected_activity),
        note="this household's usual airing",
    )


def _explain(
    best: Plan,
    habit: Plan | None,
    need: Demand,
    activity: ActivityIndex,
    mode_reason: str = "",
) -> str:
    when = dt.datetime.fromtimestamp(best.start_at, tz=dt.timezone.utc).strftime("%H:%M")
    if best.activity_band in ("high", "very high"):
        street = (
            f"even though the street is {best.activity_band} for this time of week, "
            "because this opening barely reacts to it"
        )
    else:
        street = f"while the street is {best.activity_band} for this time of week"
    parts = [
        f"{best.opening_label} at {when} for {best.duration_min:.0f} minutes, {street}."
    ]
    if habit and habit.opening_id != best.opening_id:
        parts.append(
            f"The {habit.opening_label}, which this home usually uses, is forecast to "
            f"admit {habit.pred_intake:.0f} against {best.pred_intake:.0f} ug/m3 x min."
        )
    if mode_reason:
        parts.insert(0, mode_reason)
    elif need.drivers:
        parts.append("Airing is due because " + "; ".join(need.drivers) + ".")
    return " ".join(parts)


# --- verification -----------------------------------------------------------


@dataclass
class Verification:
    plan_id: str
    realised_intake: float
    realised_exchanges: float
    predicted_intake: float
    lo: float
    hi: float
    verdict: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "realised_intake": round(self.realised_intake, 1),
            "realised_exchanges": round(self.realised_exchanges, 2),
            "predicted_intake": round(self.predicted_intake, 1),
            "lo": round(self.lo, 1),
            "hi": round(self.hi, 1),
            "verdict": self.verdict,
            "detail": self.detail,
        }


#: How far either side of an airing the sealed-home floor is measured, in hours.
FLOOR_SPAN_H = 8.0


def local_floor(
    intervals: Sequence[Interval],
    start_at: float,
    end_at: float,
    *,
    q: float = 15.0,
    back_h: float = FLOOR_SPAN_H,
    forward_h: float = FLOOR_SPAN_H,
) -> float | None:
    """Where this home settles around the time of one airing, ug/m3.

    Verification is retrospective, so it can use the readings either side of the
    airing rather than a floor carried over from whenever the model was last
    fitted. That matters: the sealed floor moves with the weather, and a
    counterfactual anchored to last month's floor measures the weather rather
    than the window.
    """
    for widen in (1.0, 2.0, 6.0):
        lo = start_at - back_h * widen * 3600.0
        hi = end_at + forward_h * widen * 3600.0
        quiet = [
            iv.c0
            for iv in intervals
            if lo <= iv.t0 <= hi and not iv.any_open and not iv.indoor_source
        ]
        if len(quiet) >= 8:
            return float(np.percentile(quiet, q))
    return None


def realised_intake(
    model: HomeModel,
    intervals: Sequence[Interval],
    opening_id: str,
    start_at: float,
    end_at: float,
) -> tuple[float, float, bool]:
    """Measure what an airing actually cost.

    The counterfactual is integrated forward from the reading before the opening
    opened, using only the sealed-home terms, and the realised intake is the area
    between it and the readings that actually arrived. Returns
    ``(intake, air_changes, complete)``.

    The forecast prices the airing plus a recovery tail, because load admitted
    through a window is still in the room after the window shuts. The tail is
    the fragile part: a household that airs at eleven and cooks at half past one
    has a perfectly measurable airing followed by a contaminated tail. Discarding
    the whole airing for that would throw away most of the record, so the tail is
    integrated only while it stays clean and the rest of it is completed at the
    home's own measured decay rate, which is exactly what the forecast assumed it
    would do. Contamination *during* the airing is different: there is then no
    airing to measure, and ``complete`` comes back False so the plan is not
    scored rather than scored badly.
    """
    tail_end = end_at + RECOVERY_MIN * 60.0
    window = [iv for iv in intervals if iv.t1 > start_at and iv.t0 < tail_end]
    if len(window) < 2:
        return 0.0, 0.0, False

    during = [iv for iv in window if iv.t0 < end_at]
    if any(iv.indoor_source for iv in during):
        return 0.0, 0.0, False
    if any(
        frac > OTHER_OPENING_TOL
        for iv in during
        for oid, frac in iv.open_frac.items()
        if oid != opening_id
    ):
        return 0.0, 0.0, False

    star = local_floor(intervals, start_at, end_at)
    if star is None:
        return 0.0, 0.0, False

    loss = model.baseline.loss_rate
    c_shadow = window[0].c0
    intake = 0.0
    exchanges = 0.0
    excess = 0.0
    covered_to = window[0].t0
    est = model.openings.get(opening_id)
    for iv in window:
        if iv.t0 >= end_at and (
            iv.indoor_source or any(f > OTHER_OPENING_TOL for f in iv.open_frac.values())
        ):
            break
        sub = max(1, int(math.ceil(iv.dt_h / 0.02)))
        h = iv.dt_h / sub
        for _ in range(sub):
            c_shadow = max(0.0, c_shadow + h * loss * (star - c_shadow))
        excess = iv.c1 - c_shadow
        intake += excess * iv.dt_h * 60.0
        covered_to = iv.t1
        if est:
            exchanges += est.exchange * iv.open_frac.get(opening_id, 0.0) * iv.dt_h

    if covered_to < end_at:
        # The tail was not the problem; the readings ran out inside the airing.
        return 0.0, 0.0, False

    remaining_h = max(0.0, tail_end - covered_to) / 3600.0
    if remaining_h > 0.0 and loss > 0.0:
        # What is left of the tail, at the decay the home was measured to have.
        intake += excess * (1.0 - math.exp(-loss * remaining_h)) / loss * 60.0
    return intake, exchanges, True


#: A plan counts as followed if the opening it named was actually open for at
#: least this fraction of the window it named.
FOLLOWED_FRACTION = 0.4


def followed_fraction(
    intervals: Sequence[Interval], opening_id: str, start_at: float, end_at: float
) -> float:
    """How much of a plan's window the named opening was actually open for."""
    total = 0.0
    span = 0.0
    for iv in intervals:
        if iv.t1 <= start_at or iv.t0 >= end_at:
            continue
        overlap = min(iv.t1, end_at) - max(iv.t0, start_at)
        if overlap <= 0:
            continue
        span += overlap
        total += overlap * iv.open_frac.get(opening_id, 0.0)
    return total / span if span > 0 else 0.0


def verify(
    model: HomeModel,
    intervals: Sequence[Interval],
    plan_row: dict,
) -> Verification | None:
    """Score a plan that has run its course.

    A plan the household did not act on is not evidence about the forecast, and
    scoring it as though it were would let SASH take credit for airings it never
    proposed and blame for ones it did. Those are recorded and shown, not
    scored.
    """
    start = plan_row["start_at"]
    end = start + plan_row["duration_min"] * 60.0
    lo = plan_row.get("pred_intake_lo") or plan_row["pred_intake"]
    hi = plan_row.get("pred_intake_hi") or plan_row["pred_intake"]

    followed = followed_fraction(intervals, plan_row["opening_id"], start, end)
    if followed < FOLLOWED_FRACTION:
        return Verification(
            plan_row["plan_id"],
            0.0,
            0.0,
            plan_row["pred_intake"],
            lo,
            hi,
            "not_followed",
            f"the {plan_row['opening_label']} was open for "
            f"{followed * 100:.0f}% of this window, so there is nothing to score",
        )

    intake, exchanges, complete = realised_intake(
        model, intervals, plan_row["opening_id"], start, end
    )
    if not complete:
        return Verification(
            plan_row["plan_id"],
            0.0,
            0.0,
            plan_row["pred_intake"],
            lo,
            hi,
            "unverifiable",
            "readings were missing, or an indoor emission ran during the airing, "
            "so this plan is not scored",
        )
    if lo <= intake <= hi:
        verdict, detail = "inside_interval", "the airing landed inside the published interval"
    elif intake > hi:
        verdict, detail = "above", "the airing cost more than SASH said it would"
    else:
        verdict, detail = "below", "the airing cost less than SASH said it would"
    return Verification(
        plan_row["plan_id"], intake, exchanges, plan_row["pred_intake"], lo, hi, verdict, detail
    )
