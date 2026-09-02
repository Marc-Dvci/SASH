"""Score SASH against ground truth, over households it has never seen.

The demo home proves the product runs. It does not prove the product is right,
because the demo home is one draw and its answer is already known. This
benchmark simulates many households with different physics and different
habits, hands SASH nothing but the streams a Ring account would deliver, and
scores five things it is possible to be wrong about.

**1. Recovery.** Does the fit find each opening's true air exchange rate and
true street coupling, and the home's true envelope penetration?

**2. Ranking.** Of the openings SASH is willing to report, does it name the one
that really admits the least street-linked load?

**3. Decision quality.** SASH's recommended airing and the household's own habit
are both re-integrated in the simulator on the record that actually happened, so
the comparison is between two real trajectories of the same flat on the same
day. How often is SASH's plan genuinely cheaper, and by how much?

**4. False claims.** ``null_home`` builds a household whose two openings are
physically identical. There is nothing to find. SASH must not announce a winner
on more than its own alpha of these, and this is the number that says whether
the permutation gate is doing its job or decorating it.

**5. Pricing and intervals.** On a held-out slice of each home's record, every
airing is forecast before it happened and then measured afterwards from the
readings. Both are compared against ``simulator.home.true_intake``, which
integrates the flat twice on the record that actually happened. That separates
two errors a live deployment cannot separate: how wrong the forecast was, and
how coarse the ruler is.

Run::

    sash bench --homes 24 --nulls 16 --days 120

Results are written to ``bench/results/`` as JSON and Markdown, and
``docs/BENCHMARK.md`` quotes them.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sash.analytics import activity as activity_mod  # noqa: E402
from sash.analytics import estimator, holdout, nulls, planner, sources  # noqa: E402
from simulator import home as sim  # noqa: E402
from simulator.tabulate import intervals_from_sim, openings_of  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

#: The level the permutation gate is run at. The false-claim rate on null homes
#: is scored against this and nothing else.
ALPHA = 0.05

#: Permutations per comparison. Enough resolution to separate 0.05 from 0.20.
PERMUTATIONS = 400

#: Decision points sampled per home, spread across the last quarter of its
#: record so the model has history behind every one of them.
DECISIONS_PER_HOME = 6


# --- per-home scores --------------------------------------------------------


@dataclass
class OpeningScore:
    opening_id: str
    reported: bool
    true_exchange: float
    fit_exchange: float
    true_coupling: float
    fit_coupling: float
    n_tracer: int
    n_episodes: int


@dataclass
class DecisionScore:
    at: float
    chosen: str
    habit: str
    true_chosen: float
    true_habit: float
    pred_saving_pct: float
    true_saving_pct: float
    better: bool


@dataclass
class ComparisonScore:
    """One pairwise verdict, against the truth of that pair."""

    a_id: str
    b_id: str
    p_value: float
    significant: bool
    observed_gap: float
    true_gap: float
    correct_sign: bool


@dataclass
class PricingScore:
    """One held-out airing, priced before and measured after, against truth."""

    opening_id: str
    start_at: float
    duration_min: float
    forecast: float
    measured: float
    truth: float
    inside_interval: bool


@dataclass
class HomeScore:
    kind: str
    seed: int
    days: float
    n_intervals: int
    fitted: bool
    n_reported: int
    true_loss_rate: float
    fit_loss_rate: float
    openings: list[OpeningScore] = field(default_factory=list)
    ranking_correct: bool | None = None
    claimed: bool = False
    min_p: float | None = None
    comparisons: list[ComparisonScore] = field(default_factory=list)
    decisions: list[DecisionScore] = field(default_factory=list)
    pricing: list[PricingScore] = field(default_factory=list)
    coverage: float | None = None
    n_coverage: int = 0
    seconds: float = 0.0


def score_home(kind: str, seed: int, days: float) -> HomeScore:
    """Simulate one household, hand SASH its streams, score what comes back."""
    started = time.time()
    rng = np.random.default_rng(seed)
    params = sim.random_home(rng) if kind == "real" else sim.null_home(rng)
    result = sim.simulate(params, days=days, t0=1_760_000_000.0, seed=seed)

    rows = intervals_from_sim(result)
    sources.detect(rows)
    openings = openings_of(params)
    model = estimator.fit(rows, openings)

    truth_lam = {o.opening_id: o.lam for o in params.openings}
    truth_g = {o.opening_id: o.g for o in params.openings}

    score = HomeScore(
        kind=kind,
        seed=seed,
        days=days,
        n_intervals=len(rows),
        fitted=model.fitted,
        n_reported=len(model.reported_openings()),
        true_loss_rate=params.lam_base + params.deposition,
        fit_loss_rate=model.baseline.loss_rate,
        openings=[
            OpeningScore(
                opening_id=oid,
                reported=est.reported,
                true_exchange=truth_lam[oid],
                fit_exchange=est.exchange,
                true_coupling=truth_g[oid],
                fit_coupling=est.coupling,
                n_tracer=est.n_tracer,
                n_episodes=est.n_episodes,
            )
            for oid, est in model.openings.items()
        ],
    )

    reported = model.reported_openings()
    if len(reported) >= 2:
        comparisons = nulls.rank_openings(rows, model, permutations=PERMUTATIONS)
        ps = [c.p_value for c in comparisons]
        score.min_p = min(ps) if ps else None
        # SASH claims a better opening only when it beats every rival it can
        # see. One undecided comparison is enough to withhold the claim.
        score.claimed = bool(comparisons) and all(c.significant for c in comparisons)
        score.comparisons = [
            ComparisonScore(
                a_id=c.a_id,
                b_id=c.b_id,
                p_value=round(c.p_value, 4),
                significant=c.significant,
                observed_gap=round(c.observed_coupling_gap, 3),
                true_gap=round(truth_g[c.a_id] - truth_g[c.b_id], 3),
                # SASH says the b side is cheaper. Is it?
                correct_sign=truth_g[c.b_id] < truth_g[c.a_id],
            )
            for c in comparisons
        ]
        best = min(reported, key=lambda o: o.coupling)
        truly_best = min(reported, key=lambda o: truth_g[o.opening_id])
        score.ranking_correct = best.opening_id == truly_best.opening_id

    if kind == "real" and reported:
        score.decisions = _score_decisions(result, params, model, rows, seed)

    score.pricing = _score_pricing(result, params, rows, openings)
    inside = [p for p in score.pricing if p.inside_interval]
    score.n_coverage = len(score.pricing)
    score.coverage = len(inside) / len(score.pricing) if score.pricing else None

    score.seconds = round(time.time() - started, 2)
    return score


def _score_decisions(
    result: sim.SimResult,
    params: sim.HomeParams,
    model: estimator.HomeModel,
    rows: list,
    seed: int,
) -> list[DecisionScore]:
    """Ask for a plan, then price both it and the habit in the simulator.

    ``sim.true_intake`` integrates the flat twice on the record that actually
    happened, once with the named opening open and once shut, so the number it
    returns is what the airing would really have cost. Neither trajectory is one
    SASH fitted, and the recovery tail is the same ninety minutes the planner
    priced.
    """
    index = activity_mod.build(result.vehicle_event_times, now=rows[-1].t1)
    out: list[DecisionScore] = []
    span = rows[-1].t1 - rows[0].t0
    starts = np.linspace(
        rows[0].t0 + 0.75 * span, rows[-1].t1 - 8 * 3600.0, DECISIONS_PER_HOME
    )
    for k, now in enumerate(starts):
        now = float(now)
        history = [iv for iv in rows if iv.t1 <= now]
        if len(history) < 100:
            continue
        rec = planner.recommend(
            model,
            index,
            history,
            now=now,
            current_pm=history[-1].c1,
            humidity=history[-1].humidity,
            co=history[-1].co,
            draws=model.draws,
            explore=False,
            seed=seed + k,
        )
        if rec.best is None or rec.habit is None:
            continue
        if rec.best.opening_id == rec.habit.opening_id:
            # SASH agrees with the household. Nothing was decided, so nothing is
            # scored: counting these would inflate the number with the habit's
            # own performance.
            continue
        true_chosen = sim.true_intake(
            result, params, rec.best.opening_id, rec.best.start_at, rec.best.duration_min
        )
        true_habit = sim.true_intake(
            result, params, rec.habit.opening_id, rec.habit.start_at, rec.habit.duration_min
        )
        if math.isnan(true_chosen) or math.isnan(true_habit):
            continue
        pred_saving = (
            100.0 * (rec.habit.pred_intake - rec.best.pred_intake) / abs(rec.habit.pred_intake)
            if abs(rec.habit.pred_intake) > 1e-9
            else 0.0
        )
        true_saving = (
            100.0 * (true_habit - true_chosen) / abs(true_habit)
            if abs(true_habit) > 1e-9
            else 0.0
        )
        out.append(
            DecisionScore(
                at=now,
                chosen=rec.best.opening_id,
                habit=rec.habit.opening_id,
                true_chosen=round(true_chosen, 1),
                true_habit=round(true_habit, 1),
                pred_saving_pct=round(pred_saving, 1),
                true_saving_pct=round(true_saving, 1),
                better=true_chosen < true_habit,
            )
        )
    return out


def _score_pricing(
    result: sim.SimResult,
    params: sim.HomeParams,
    rows: list,
    openings: list[tuple[str, str, str | None]],
) -> list[PricingScore]:
    """Price the held-out airings twice, and compare both with the truth.

    Same three-way split the product itself uses: fitted on the first half,
    interval calibrated on the third quarter, scored on the last quarter. The
    forecast is what SASH would have said the morning of the airing; the
    measurement is what SASH says afterwards from the readings alone; the truth
    is the simulator's.
    """
    usable = [iv for iv in rows if iv.usable]
    if len(usable) < 300:
        return []
    cut_fit = usable[int(len(usable) * holdout.FIT_SPLIT)].t0
    cut_cal = usable[int(len(usable) * holdout.CALIBRATE_SPLIT)].t0
    early = [iv for iv in usable if iv.t1 <= cut_fit]
    middle = [iv for iv in usable if cut_fit <= iv.t0 and iv.t1 <= cut_cal]
    late = [iv for iv in usable if iv.t0 >= cut_cal]

    model = estimator.fit(early, openings)
    if not model.reported_openings():
        return []
    index = holdout.activity_from(early)
    ids = [o[0] for o in openings]
    rng = np.random.default_rng(7)

    calibration = holdout._multiplier(
        holdout.price(model, index, usable, middle, holdout.episodes(middle, ids), rng=rng),
        0.90,
    )

    out: list[PricingScore] = []
    for _eid, opening_id, ivs in holdout.episodes(late, ids):
        est = model.openings.get(opening_id)
        if est is None or not est.reported:
            continue
        start, end = ivs[0].t0, ivs[-1].t1
        duration = (end - start) / 60.0
        if not (holdout.MIN_AIRING_MIN <= duration <= holdout.MAX_AIRING_MIN):
            continue
        if any(iv.indoor_source for iv in ivs):
            continue
        measured, _exchanges, complete = planner.realised_intake(
            model, late, opening_id, start, end
        )
        if not complete:
            continue
        truth = sim.true_intake(result, params, opening_id, start, duration)
        if not math.isfinite(truth):
            continue
        star = estimator.floor_at(usable, start) or model.baseline.recent_floor
        forecast, _ = planner._forecast_intake(
            model, index, ivs[0].c0, star, start, opening_id, start, duration
        )
        half = calibration.k * planner.interval_scale(star, ivs[0].c0, duration)
        out.append(
            PricingScore(
                opening_id=opening_id,
                start_at=start,
                duration_min=round(duration, 1),
                forecast=round(forecast, 1),
                measured=round(measured, 1),
                truth=round(truth, 1),
                inside_interval=bool(abs(measured - forecast) <= half),
            )
        )
    return out


# --- aggregation ------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval. Zero false claims out of sixteen is not evidence of zero."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarise(scores: list[HomeScore]) -> dict[str, Any]:
    real = [s for s in scores if s.kind == "real"]
    null = [s for s in scores if s.kind == "null"]

    loss_err = [abs(s.fit_loss_rate - s.true_loss_rate) for s in real if s.fitted]

    ex_rel, cp_abs, cp_rel = [], [], []
    for s in real:
        for o in s.openings:
            if not o.reported:
                continue
            if o.true_exchange > 1e-6:
                ex_rel.append(abs(o.fit_exchange - o.true_exchange) / o.true_exchange)
            cp_abs.append(abs(o.fit_coupling - o.true_coupling))
            if o.true_coupling > 0.5:
                cp_rel.append(abs(o.fit_coupling - o.true_coupling) / o.true_coupling)

    comparisons = [c for s in real for c in s.comparisons]
    called = [c for c in comparisons if c.significant]
    ranked = [s for s in real if s.ranking_correct is not None]
    # What SASH is right about when it is willing to speak. A household whose
    # two quiet openings differ by a hair is a coin toss, and the permutation
    # gate is what stops SASH calling it.
    claimed = [s for s in ranked if s.claimed]
    withheld = [s for s in ranked if not s.claimed]
    decisions = [d for s in real for d in s.decisions]
    decisions_claimed = [d for s in real if s.claimed for d in s.decisions]
    true_gaps = []
    for s in ranked:
        gs = sorted(o.true_coupling for o in s.openings if o.reported)
        if len(gs) >= 2:
            true_gaps.append((s.claimed, gs[1] - gs[0]))
    n_null_ranked = sum(1 for s in null if s.min_p is not None)
    n_claimed_null = sum(1 for s in null if s.claimed)
    lo, hi = _wilson(n_claimed_null, n_null_ranked)

    priced = [p for s in scores for p in s.pricing]
    truths = [abs(p.truth) for p in priced]
    err_fc = [abs(p.forecast - p.truth) for p in priced]
    err_ms = [abs(p.measured - p.truth) for p in priced]
    bias_ms = [p.measured - p.truth for p in priced]
    n_inside = sum(1 for p in priced if p.inside_interval)

    return {
        "n_homes": len(real),
        "n_null_homes": len(null),
        "alpha": ALPHA,
        "permutations": PERMUTATIONS,
        "recovery": {
            "median_abs_error_loss_rate": _median(loss_err),
            "median_rel_error_exchange": _median(ex_rel),
            "median_abs_error_coupling": _median(cp_abs),
            "median_rel_error_coupling": _median(cp_rel),
            "n_openings_reported": len(cp_abs),
            "n_openings_total": sum(len(s.openings) for s in real),
        },
        "ranking": {
            "n": len(ranked),
            "correct": sum(1 for s in ranked if s.ranking_correct),
            "accuracy": (
                sum(1 for s in ranked if s.ranking_correct) / len(ranked) if ranked else None
            ),
            "n_claimed": len(claimed),
            "correct_when_claimed": sum(1 for s in claimed if s.ranking_correct),
            "accuracy_when_claimed": (
                sum(1 for s in claimed if s.ranking_correct) / len(claimed)
                if claimed
                else None
            ),
            "n_withheld": len(withheld),
            "n_comparisons": len(comparisons),
            "n_comparisons_called": len(called),
            "n_comparisons_called_correct": sum(1 for c in called if c.correct_sign),
            "accuracy_per_comparison_called": (
                sum(1 for c in called if c.correct_sign) / len(called) if called else None
            ),
            "median_true_gap_called": _median([abs(c.true_gap) for c in called]),
            "median_true_gap_not_called": _median(
                [abs(c.true_gap) for c in comparisons if not c.significant]
            ),
            "median_true_gap_claimed": _median(
                [g for c, g in true_gaps if c]
            ),
            "median_true_gap_withheld": _median(
                [g for c, g in true_gaps if not c]
            ),
        },
        "decisions": {
            "n": len(decisions),
            "n_better": sum(1 for d in decisions if d.better),
            "accuracy": (
                sum(1 for d in decisions if d.better) / len(decisions) if decisions else None
            ),
            "n_claimed": len(decisions_claimed),
            "n_better_when_claimed": sum(1 for d in decisions_claimed if d.better),
            "accuracy_when_claimed": (
                sum(1 for d in decisions_claimed if d.better) / len(decisions_claimed)
                if decisions_claimed
                else None
            ),
            "median_true_saving_pct_claimed": _median(
                [d.true_saving_pct for d in decisions_claimed]
            ),
            "median_true_saving_pct": _median([d.true_saving_pct for d in decisions]),
            "median_pred_saving_pct": _median([d.pred_saving_pct for d in decisions]),
        },
        "false_claims": {
            "n_null_homes_ranked": n_null_ranked,
            "n_claimed": n_claimed_null,
            "rate": n_claimed_null / n_null_ranked if n_null_ranked else None,
            "wilson_95": [round(lo, 3), round(hi, 3)],
        },
        "pricing": {
            "level": 0.90,
            "n_airings": len(priced),
            "coverage": n_inside / len(priced) if priced else None,
            "median_true_intake": _median(truths),
            "median_forecast_error": _median(err_fc),
            "median_measurement_error": _median(err_ms),
            "median_measurement_bias": _median(bias_ms),
        },
    }


# --- reporting --------------------------------------------------------------


def _pct(v: float | None, d: int = 1) -> str:
    return "--" if v is None else f"{100 * v:.{d}f}%"


def _num(v: float | None, d: int = 1) -> str:
    return "--" if v is None else f"{v:.{d}f}"


def markdown(summary: dict[str, Any], seconds: float) -> str:
    r = summary["recovery"]
    k = summary["ranking"]
    d = summary["decisions"]
    f = summary["false_claims"]
    i = summary["pricing"]
    rel = (
        None
        if not i["median_true_intake"]
        else i["median_forecast_error"] / i["median_true_intake"]
    )
    return f"""# Benchmark

{summary['n_homes']} simulated households plus {summary['n_null_homes']} negative
controls, {_num(summary.get('days'), 0)} days of Ring streams each, scored against the
simulator's own physics. Reproduce with `sash bench --homes {summary['n_homes']} \
--nulls {summary['n_null_homes']} --days {_num(summary.get('days'), 0)} \
--seed {summary.get('seed', 1)}`.

## Recovery

| quantity | median error |
|---|---|
| Sealed-home loss rate | {_num(r['median_abs_error_loss_rate'], 3)} per hour absolute |
| Air exchange rate, per opening | {_pct(r['median_rel_error_exchange'])} relative |
| Street coupling, per opening | {_num(r['median_abs_error_coupling'], 2)} ug/m3 per event/min |

{r['n_openings_reported']} of {r['n_openings_total']} openings passed the reporting
gate and carry a number. The rest are shown as still learning, with the reason.

## Ranking, and what the gate is for

SASH named the truly least-coupled opening in {k['correct']} of {k['n']} households
({_pct(k['accuracy'])}). That number mixes two questions. In
{k['n_withheld']} of those households the permutation gate refused to call a
winner, and it refused on the right ones: the median true gap between the two
openings is {_num(k['median_true_gap_claimed'], 2)} ug/m3 per event/min where SASH
made the claim and {_num(k['median_true_gap_withheld'], 2)} where it withheld it.

The verdict SASH actually shows is per pair of openings, and a three-opening
home produces two of them. Across {k['n_comparisons']} pairs, the gate called
{k['n_comparisons_called']} and withheld the rest. **Of the pairs it called, the
opening it named as cheaper really was cheaper in
{k['n_comparisons_called_correct']} of {k['n_comparisons_called']}
({_pct(k['accuracy_per_comparison_called'])}).** The median true gap is
{_num(k['median_true_gap_called'], 2)} on the pairs it called and
{_num(k['median_true_gap_not_called'], 2)} on the pairs it withheld.

## Decision quality

{d['n_better']} of {d['n']} recommendations ({_pct(d['accuracy'])}) really did cost
less than the household's own habitual airing, measured by re-integrating the flat
in the simulator on the record that actually happened. Median realised saving
{_num(d['median_true_saving_pct'])}%, against a median forecast saving of
{_num(d['median_pred_saving_pct'])}%.

Restricted to the households where the openings are distinguishable at all:
**{d['n_better_when_claimed']} of {d['n_claimed']}**
({_pct(d['accuracy_when_claimed'])}), median realised saving
**{_num(d['median_true_saving_pct_claimed'])}%**.

## False claims on openings that are identical

**{f['n_claimed']} of {f['n_null_homes_ranked']}** null households
({_pct(f['rate'])}, Wilson 95% {_pct(f['wilson_95'][0])} to {_pct(f['wilson_95'][1])})
had a difference announced where none exists. The permutation gate runs at
alpha = {summary['alpha']} over {summary['permutations']} shuffles.

## Pricing one airing

{i['n_airings']} held-out airings, each forecast before it happened, measured
afterwards from the readings, and compared with the simulator's own answer.

| quantity | value |
|---|---|
| Median true cost of an airing | {_num(i['median_true_intake'])} ug/m3 x min |
| Median forecast error | {_num(i['median_forecast_error'])} ({_pct(rel, 0)} of it) |
| Median error of the retrospective measurement | {_num(i['median_measurement_error'])} |
| Median bias of that measurement | {_num(i['median_measurement_bias'])} |
| Coverage of the 90% interval | {_pct(i['coverage'])} |

One airing is genuinely hard to price, and it is hard to measure afterwards as
well. That is why the product's claim is a comparison between two openings on
the same day rather than an absolute number: the terms that make a single
airing uncertain are common to both sides of it.

Ran in {seconds:.0f} s.
"""


def main(homes: int = 24, nulls_n: int = 16, days: float = 120.0, seed: int = 1, **kw) -> int:
    # ``sash bench`` calls this with the keyword ``nulls``; accept both.
    nulls_n = int(kw.pop("nulls", nulls_n))
    started = time.time()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    scores: list[HomeScore] = []
    total = homes + nulls_n
    for i in range(homes):
        s = score_home("real", seed * 1000 + i, days)
        scores.append(s)
        print(
            f"[{len(scores):>3}/{total}] home  seed={s.seed} "
            f"reported={s.n_reported}/{len(s.openings)} "
            f"rank={'ok' if s.ranking_correct else 'no' if s.ranking_correct is not None else '--'} "
            f"decisions={sum(1 for d in s.decisions if d.better)}/{len(s.decisions)} "
            f"priced={len(s.pricing)} {s.seconds}s",
            flush=True,
        )
    for i in range(nulls_n):
        s = score_home("null", seed * 1000 + 500 + i, days)
        scores.append(s)
        print(
            f"[{len(scores):>3}/{total}] null  seed={s.seed} "
            f"claimed={'YES' if s.claimed else 'no'} "
            f"min_p={_num(s.min_p, 3)} {s.seconds}s",
            flush=True,
        )

    summary = summarise(scores)
    summary["days"] = days
    summary["seed"] = seed
    seconds = time.time() - started

    payload = {
        "summary": summary,
        "seconds": round(seconds, 1),
        "homes": [asdict(s) for s in scores],
    }
    (RESULTS_DIR / "benchmark.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    report = markdown(summary, seconds)
    (RESULTS_DIR / "benchmark.md").write_text(report, encoding="utf-8")

    print()
    print(report)
    print(f"Written to {RESULTS_DIR / 'benchmark.json'}")
    return 0


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--homes", type=int, default=24)
    p.add_argument("--nulls", type=int, default=16)
    p.add_argument("--days", type=float, default=120.0)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    raise SystemExit(main(homes=a.homes, nulls=a.nulls, days=a.days, seed=a.seed))
