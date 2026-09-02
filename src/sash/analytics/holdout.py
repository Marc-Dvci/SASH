"""What SASH would have said, scored against what the home then measured.

The ledger on the Record tab holds the plans SASH published while it was
running. That is the right accountability surface, and it is also a very short
list on the day a household installs SASH. This module fills it with something
older and harder: a held-out replay of the home's own record.

The record is cut by time. The model is fitted on the earliest part and never
sees the rest. Then every airing the household actually took later is forecast
**before it happened**, from the fitted model and the street's weekly habit
alone, and compared with what the readings afterwards showed it cost. The airing
itself is the household's; SASH is only being asked whether it can price one.

Two things come out of that, and they are different jobs done by different
slices of the record.

**Calibration.** The interval around a forecast cannot come from the parameter
bootstrap alone. The bootstrap knows how uncertain the exchange rate and the
street coupling are; it does not know that next Tuesday's regional air will be
dirtier than this Tuesday's. So the parametric half-width is used as a shape,
and its size is set by how far this household's own past airings landed from
their forecasts. That is a split-conformal calibration, and it is the difference
between an interval that is arithmetically correct and one that is true.

**Evaluation.** Coverage is then measured on a third slice that neither the fit
nor the calibration has seen, so the number reported is not the number that was
optimised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from . import activity as activity_mod
from . import estimator, planner
from .intervals import Interval

#: Where the record is cut. The model is fitted below ``FIT_SPLIT``, the
#: interval is calibrated between the two, and coverage is measured above
#: ``CALIBRATE_SPLIT``.
FIT_SPLIT = 0.5
CALIBRATE_SPLIT = 0.75

#: Split used when calibrating for live use, where there is no third slice to
#: hold back: everything after it is calibration.
LIVE_SPLIT = 0.7

#: An airing has to be at least this long to be worth pricing.
MIN_AIRING_MIN = 8.0

#: Airings longer than this are not scored: the forecast is a ventilation
#: forecast, not a model of a window left open all afternoon.
MAX_AIRING_MIN = 120.0

#: Draws used per airing when shaping its interval.
N_DRAWS = 40


@dataclass
class Calibration:
    """How much wider than the parameter bootstrap the interval has to be."""

    k: float = planner.UNCALIBRATED_K
    n: int = 0
    level: float = 0.90
    fitted: bool = False

    def as_dict(self) -> dict:
        return {
            "k": round(self.k, 2),
            "n": self.n,
            "level": self.level,
            "fitted": self.fitted,
        }


@dataclass
class ScoredAiring:
    opening_id: str
    opening_label: str
    start_at: float
    duration_min: float
    pred_intake: float
    pred_lo: float
    pred_hi: float
    realised_intake: float
    verdict: str

    def as_dict(self) -> dict:
        return {
            "opening_id": self.opening_id,
            "opening_label": self.opening_label,
            "start_at": self.start_at,
            "duration_min": round(self.duration_min, 1),
            "pred_intake": round(self.pred_intake, 1),
            "pred_intake_lo": round(self.pred_lo, 1),
            "pred_intake_hi": round(self.pred_hi, 1),
            "realised_intake": round(self.realised_intake, 1),
            "verdict": self.verdict,
        }


@dataclass
class Backtest:
    split_at: float = 0.0
    fit_days: float = 0.0
    held_days: float = 0.0
    n_airings: int = 0
    n_scored: int = 0
    n_inside: int = 0
    coverage: float | None = None
    median_abs_error: float | None = None
    median_realised: float | None = None
    calibration: Calibration = field(default_factory=Calibration)
    ranking_held: bool = False
    ranking_note: str = ""
    airings: list[ScoredAiring] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "split_at": self.split_at,
            "fit_days": round(self.fit_days, 1),
            "held_days": round(self.held_days, 1),
            "n_airings": self.n_airings,
            "n_scored": self.n_scored,
            "n_inside": self.n_inside,
            "coverage": None if self.coverage is None else round(self.coverage, 3),
            "median_abs_error": (
                None if self.median_abs_error is None else round(self.median_abs_error, 1)
            ),
            "median_realised": (
                None if self.median_realised is None else round(self.median_realised, 1)
            ),
            "calibration": self.calibration.as_dict(),
            "ranking_held": self.ranking_held,
            "ranking_note": self.ranking_note,
            "airings": [a.as_dict() for a in self.airings],
        }


# --- pieces -----------------------------------------------------------------


def episodes(
    rows: Sequence[Interval], opening_ids: Sequence[str]
) -> list[tuple[str, str, list[Interval]]]:
    """Airings, as ``(episode_id, opening_id, its intervals)``, one opening only."""
    groups: dict[str, list[Interval]] = {}
    for iv in rows:
        if iv.episode_id:
            groups.setdefault(iv.episode_id, []).append(iv)
    out = []
    for eid, ivs in groups.items():
        opening_id = eid.rsplit(":", 1)[0]
        if opening_id not in opening_ids:
            continue
        others = [o for o in opening_ids if o != opening_id]
        if any(iv.open_frac.get(o, 0.0) > 0.15 for iv in ivs for o in others):
            continue
        out.append((eid, opening_id, sorted(ivs, key=lambda x: x.t0)))
    out.sort(key=lambda x: x[2][0].t0)
    return out


def activity_from(rows: Sequence[Interval]) -> activity_mod.ActivityIndex:
    """Rebuild a street index from an interval table's own activity column.

    The backtest deliberately does not reach back for the raw event times: the
    later part of the record is untouched, and that includes the camera. Each
    interval carries the event rate measured over it, which is all the weekly
    profile the planner forecasts from needs.
    """
    index = activity_mod.ActivityIndex(times=[])
    if not rows:
        return index
    slots = activity_mod.SLOTS_PER_DAY
    weekday_bins: list[list[float]] = [[] for _ in range(slots)]
    weekend_bins: list[list[float]] = [[] for _ in range(slots)]
    rates: list[float] = []
    for iv in rows:
        if not iv.camera_online:
            continue
        slot = int((iv.hour * 60) // activity_mod.PROFILE_SLOT_MIN) % slots
        (weekday_bins if iv.weekday else weekend_bins)[slot].append(iv.activity)
        rates.append(iv.activity)
    index.weekday_profile = np.array(
        [float(np.median(v)) if v else 0.0 for v in weekday_bins]
    )
    index.weekend_profile = np.array(
        [float(np.median(v)) if v else 0.0 for v in weekend_bins]
    )
    index.ratio_grid = activity_mod.ratio_grid(weekday_bins, weekend_bins)
    index.percentile_grid = np.sort(np.array(rates)) if rates else np.array([])
    index.n_events = len(rates)
    return index


@dataclass
class Priced:
    opening_id: str
    label: str
    start_at: float
    duration_min: float
    pred: float
    half_width: float
    realised: float


def price(
    model: estimator.HomeModel,
    index: activity_mod.ActivityIndex,
    history: Sequence[Interval],
    scoring_rows: Sequence[Interval],
    airings: Sequence[tuple[str, str, list[Interval]]],
    *,
    rng: np.random.Generator,
) -> list[Priced]:
    """Forecast each airing from what was known before it, then measure it.

    ``history`` is everything the forecast is allowed to look back at, and it is
    read backwards only. ``scoring_rows`` is what the realised cost is measured
    against afterwards.
    """
    out: list[Priced] = []
    fallback_star = model.baseline.recent_floor
    for _eid, opening_id, ivs in airings:
        est = model.openings.get(opening_id)
        if est is None or not est.reported:
            continue
        start = ivs[0].t0
        end = ivs[-1].t1
        duration = (end - start) / 60.0
        if not (MIN_AIRING_MIN <= duration <= MAX_AIRING_MIN):
            continue
        if any(iv.indoor_source for iv in ivs):
            continue

        # The floor the home was sitting at when the decision would have been
        # made, on the same definition the fit uses and measured backwards only.
        star_now = estimator.floor_at(history, start)
        if star_now is None:
            star_now = fallback_star

        c0 = ivs[0].c0
        pred, _ = planner._forecast_intake(
            model, index, c0, star_now, start, opening_id, start, duration
        )

        realised, _exchanges, complete = planner.realised_intake(
            model, scoring_rows, opening_id, start, end
        )
        if not complete:
            continue
        out.append(
            Priced(
                opening_id=opening_id,
                label=est.label,
                start_at=start,
                duration_min=duration,
                pred=pred,
                half_width=planner.interval_scale(star_now, c0, duration),
                realised=realised,
            )
        )
    return out


def _multiplier(priced: Sequence[Priced], level: float) -> Calibration:
    if len(priced) < 8:
        return Calibration(k=planner.UNCALIBRATED_K, n=len(priced), level=level)
    z = np.array([abs(p.realised - p.pred) / max(p.half_width, 1e-6) for p in priced])
    return Calibration(k=float(np.quantile(z, level)), n=int(z.size), level=level, fitted=True)


def calibrate(
    intervals: Sequence[Interval],
    openings: Sequence[tuple[str, str, str | None]],
    *,
    split: float = LIVE_SPLIT,
    level: float = 0.90,
    seed: int = 23,
) -> Calibration:
    """How wide this household's forecast intervals have to be to be true.

    Fit on the earlier part of its record, price the airings in the later part,
    and take the quantile of how far they landed from their forecasts, in units
    of the parametric half-width. Nothing about the airings being priced reaches
    the fit.
    """
    rows = [iv for iv in intervals if iv.usable]
    if len(rows) < 200 or not openings:
        return Calibration(level=level)

    cut = rows[int(len(rows) * split)].t0
    early = [iv for iv in rows if iv.t1 <= cut]
    late = [iv for iv in rows if iv.t0 >= cut]
    model = estimator.fit(early, openings)
    if not model.reported_openings():
        return Calibration(level=level)
    index = activity_from(early)
    ids = [o[0] for o in openings]
    priced = price(
        model, index, rows, late, episodes(late, ids), rng=np.random.default_rng(seed)
    )
    return _multiplier(priced, level)


def run(
    intervals: Sequence[Interval],
    openings: Sequence[tuple[str, str, str | None]],
    *,
    fit_split: float = FIT_SPLIT,
    calibrate_split: float = CALIBRATE_SPLIT,
    level: float = 0.90,
    max_airings: int = 60,
    seed: int = 23,
) -> Backtest:
    """Fit, calibrate and score on three slices of the record, in that order."""
    rows = [iv for iv in intervals if iv.usable]
    result = Backtest()
    if len(rows) < 300 or not openings:
        result.ranking_note = "not enough history to hold any of it out yet"
        return result

    cut_fit = rows[int(len(rows) * fit_split)].t0
    cut_cal = rows[int(len(rows) * calibrate_split)].t0
    early = [iv for iv in rows if iv.t1 <= cut_fit]
    middle = [iv for iv in rows if cut_fit <= iv.t0 and iv.t1 <= cut_cal]
    late = [iv for iv in rows if iv.t0 >= cut_cal]
    result.split_at = cut_cal
    result.fit_days = (early[-1].t1 - early[0].t0) / 86400.0 if early else 0.0
    result.held_days = (late[-1].t1 - late[0].t0) / 86400.0 if late else 0.0

    model = estimator.fit(early, openings)
    if not model.reported_openings():
        result.ranking_note = "the fitting slice did not measure any opening well enough"
        return result

    index = activity_from(early)
    ids = [o[0] for o in openings]
    rng = np.random.default_rng(seed)

    calibration = _multiplier(
        price(model, index, rows, middle, episodes(middle, ids), rng=rng), level
    )
    result.calibration = calibration

    airings = episodes(late, ids)
    result.n_airings = len(airings)
    priced = price(model, index, rows, late, airings, rng=rng)

    scored: list[ScoredAiring] = []
    for p in priced:
        lo = p.pred - calibration.k * p.half_width
        hi = p.pred + calibration.k * p.half_width
        if lo <= p.realised <= hi:
            verdict = "inside_interval"
        elif p.realised > hi:
            verdict = "above"
        else:
            verdict = "below"
        scored.append(
            ScoredAiring(
                opening_id=p.opening_id,
                opening_label=p.label,
                start_at=p.start_at,
                duration_min=p.duration_min,
                pred_intake=p.pred,
                pred_lo=lo,
                pred_hi=hi,
                realised_intake=p.realised,
                verdict=verdict,
            )
        )

    result.n_scored = len(scored)
    result.n_inside = sum(1 for a in scored if a.verdict == "inside_interval")
    if scored:
        result.coverage = result.n_inside / result.n_scored
        result.median_abs_error = float(
            np.median([abs(a.realised_intake - a.pred_intake) for a in scored])
        )
        result.median_realised = float(np.median([abs(a.realised_intake) for a in scored]))
    scored.sort(key=lambda a: a.start_at, reverse=True)
    result.airings = scored[:max_airings]

    result.ranking_held, result.ranking_note = ranking_note(model, intervals, openings)
    return result


def ranking_note(
    model: estimator.HomeModel,
    intervals: Sequence[Interval],
    openings: Sequence[tuple[str, str, str | None]],
) -> tuple[bool, str]:
    """Did the opening the fitting slice preferred stay preferred on the whole record?"""
    reported = model.reported_openings()
    if len(reported) < 2:
        return False, "the fitting slice measured fewer than two openings"
    early_best = min(reported, key=lambda o: o.coupling)
    full = estimator.fit(intervals, openings, bootstrap=0)
    full_reported = full.reported_openings()
    if len(full_reported) < 2:
        return False, "the full record measures fewer than two openings"
    full_best = min(full_reported, key=lambda o: o.coupling)
    if full_best.opening_id == early_best.opening_id:
        return True, (
            f"the {early_best.label} was the cheapest opening on the fitting "
            "slice of the record and still is on all of it"
        )
    return False, (
        f"the fitting slice preferred the {early_best.label}; the whole record "
        f"prefers the {full_best.label}"
    )
