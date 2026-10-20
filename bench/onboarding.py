"""How long a new household waits for its first plan, with and without guidance.

A Ring sensor integration has no history to backfill, so SASH's first weeks are
spent earning the airings its reporting gate asks for. This benchmark runs each
household twice on the same simulated world -- the same street, weather,
cooking and habit -- and refits SASH every morning:

* **Habit.** The household airs as it always has.
* **Guided.** Each morning SASH shows the calibration steps from
  :mod:`sash.analytics.onboarding`, and the household follows each one with
  probability ``--comply``. A step asks for an airing the household would make
  anyway (after cooking, or at a time it already airs), through a named window,
  with the others shut. The simulated household reads the step the way a person
  would: it knows when it has finished cooking, and it opens the window it was
  asked to open instead of the one it would have reached for.

Three milestones are scored, in days since the account was linked: the first
plan SASH can make (one window measured), every window measured, and the first
pair of windows SASH is willing to call.

Run::

    python bench/onboarding.py --homes 24 --days 45 --comply 0.6

Results are written to ``bench/results/onboarding.json`` and ``.md``.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sash.analytics import activity as activity_mod  # noqa: E402
from sash.analytics import estimator, nulls, onboarding, sources  # noqa: E402
from simulator import home as sim  # noqa: E402
from simulator.tabulate import intervals_from_sim, openings_of  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
T0 = 1_760_000_000.0

#: Permutations for the daily "can SASH call a pair yet" check. Coarser than the
#: main benchmark's 400, because it runs every few days per home.
PERMUTATIONS = 200

#: How often, in days, the pairwise check is run once every window is measured.
CALL_EVERY_DAYS = 3


@dataclass
class Onboarding:
    seed: int
    policy: str
    n_openings: int
    first_plan_day: int | None = None
    all_measured_day: int | None = None
    first_call_day: int | None = None
    steps_shown: int = 0
    airings_followed: int = 0
    seconds: float = 0.0


def _rows_until(result: sim.SimResult, now: float) -> list:
    """The observation table as it stood at ``now``: no reading from the future."""
    past = dataclasses.replace(
        result, aqm_reports=[r for r in result.aqm_reports if r["reported_at"] <= now]
    )
    rows = intervals_from_sim(past)
    sources.detect(rows)
    return rows


def _meal_ends(result: sim.SimResult, lo: float, hi: float) -> list[float]:
    """When the household finished cooking, between ``lo`` and ``hi``."""
    co = result.source_co
    m0 = max(1, int((lo - result.t0) / 60.0))
    m1 = min(result.minutes, int((hi - result.t0) / 60.0))
    return [result.time_at(m) for m in range(m0, m1) if co[m - 1] > 0 and co[m] == 0]


def _respond(
    result: sim.SimResult,
    steps: list[onboarding.Step],
    now: float,
    rng: np.random.Generator,
    comply: float,
) -> list[tuple[str, float, float]]:
    """What the household actually does today with the steps it was shown."""
    airings: list[tuple[str, float, float]] = []
    # Steps that want an airing after cooking, or any airing, take turns over
    # the day's meals: one meal, one window.
    rotation = [s for s in steps if s.kind in ("after_cooking", "any")]
    meals = _meal_ends(result, now, now + 86400.0)
    for k, end in enumerate(meals):
        if not rotation or rng.random() > comply:
            continue
        step = rotation[k % len(rotation)]
        airings.append((step.opening_id, end + float(rng.uniform(0, 600)), step.duration_min))
    for step in steps:
        if step.kind in ("busy_street", "quiet_street") and step.suggested_at is not None:
            if rng.random() <= comply:
                airings.append((step.opening_id, step.suggested_at, step.duration_min))
    return airings


def run_home(seed: int, days: int, policy: str, comply: float) -> Onboarding:
    started = time.time()
    rng = np.random.default_rng(seed)
    params = sim.random_home(rng)
    result = sim.simulate(params, days=days, t0=T0, seed=seed)
    openings = openings_of(params)
    out = Onboarding(seed=seed, policy=policy, n_openings=len(openings))
    respond_rng = np.random.default_rng(seed + 7_919)

    for day in range(1, days + 1):
        now = T0 + day * 86400.0
        rows = _rows_until(result, now)
        model = estimator.fit(rows, openings, bootstrap=20)
        reported = model.reported_openings()
        if reported and out.first_plan_day is None:
            out.first_plan_day = day
        if len(reported) == len(openings) and out.all_measured_day is None:
            out.all_measured_day = day
        if (
            out.all_measured_day is not None
            and out.first_call_day is None
            and (day - out.all_measured_day) % CALL_EVERY_DAYS == 0
        ):
            comparisons = nulls.rank_openings(rows, model, permutations=PERMUTATIONS)
            if any(c.significant for c in comparisons):
                out.first_call_day = day
        if out.first_call_day is not None:
            break

        if policy == "guided" and len(reported) < len(openings):
            index = activity_mod.build(
                [t for t in result.vehicle_event_times if t <= now], now=now
            )
            steps = onboarding.steps(model, rows, index, now)
            out.steps_shown += len(steps)
            airings = _respond(result, steps, now, respond_rng, comply)
            out.airings_followed += len(airings)
            sim.household_airs(result, params, airings, seed=seed + day)

    out.seconds = round(time.time() - started, 1)
    return out


def _median_days(values: list[int | None], days: int) -> tuple[float | None, int]:
    """Median day, counting a home that never got there as ``days + 1``."""
    if not values:
        return None, 0
    reached = sum(1 for v in values if v is not None)
    filled = [v if v is not None else days + 1 for v in values]
    return float(statistics.median(filled)), reached


def summarise(scores: list[Onboarding], days: int) -> dict[str, Any]:
    out: dict[str, Any] = {"days": days}
    for policy in ("habit", "guided"):
        ss = [s for s in scores if s.policy == policy]
        block: dict[str, Any] = {"n": len(ss)}
        for key in ("first_plan_day", "all_measured_day", "first_call_day"):
            med, reached = _median_days([getattr(s, key) for s in ss], days)
            block[key] = {"median": med, "reached": reached}
            for cut in (7, 14, 21, 30):
                block[key][f"by_day_{cut}"] = sum(
                    1 for s in ss if getattr(s, key) is not None and getattr(s, key) <= cut
                )
        followed = [
            s.airings_followed / s.all_measured_day
            for s in ss
            if s.all_measured_day
        ]
        block["airings_followed_per_day"] = (
            float(statistics.median(followed)) if followed else None
        )
        out[policy] = block
    pairs = {}
    for s in scores:
        pairs.setdefault(s.seed, {})[s.policy] = s
    # A home that never got there counts as day ``days + 1``, so the paired
    # difference is a lower bound wherever the habit run never finished.
    def reached(s: Onboarding) -> int:
        return s.all_measured_day if s.all_measured_day is not None else days + 1

    faster = [
        reached(p["habit"]) - reached(p["guided"])
        for p in pairs.values()
        if "habit" in p and "guided" in p
    ]
    out["paired_days_saved_all_measured"] = (
        float(statistics.median(faster)) if faster else None
    )
    out["n_guided_faster"] = sum(1 for f in faster if f > 0)
    out["n_guided_slower"] = sum(1 for f in faster if f < 0)
    out["n_paired"] = len(faster)
    return out


def _d(v: float | None) -> str:
    return "--" if v is None else f"{v:.0f}"


def markdown(summary: dict[str, Any], comply: float, homes: int, seconds: float) -> str:
    h, g = summary["habit"], summary["guided"]
    days = summary["days"]

    def row(name: str, key: str) -> str:
        return (
            f"| {name} | {_d(h[key]['median'])} | **{_d(g[key]['median'])}** | "
            f"{h[key]['by_day_14']} to **{g[key]['by_day_14']}** | "
            f"{h[key]['by_day_21']} to **{g[key]['by_day_21']}** | "
            f"{h[key]['reached']} to {g[key]['reached']} |"
        )

    return f"""# Onboarding

{homes} simulated households, each run twice on the same world for up to {days}
days: once on its own habit, once following SASH's calibration steps with
probability {comply:.0%} per step. SASH is refitted every morning on the
readings so far. Reproduce with `python bench/onboarding.py --homes {homes} \
--days {days} --comply {comply}`.

| milestone, days since linking | habit, median | guided, median | homes by day 14 | homes by day 21 | homes by day {days} |
|---|---|---|---|---|---|
{row("First plan (one window measured)", "first_plan_day")}
{row("Every window measured", "all_measured_day")}
{row("First pair of windows called", "first_call_day")}

A home that had not reached a milestone by day {days} counts as day {days + 1} in
the median. Paired on the same home, guidance measured every window a median
of **{_d(summary['paired_days_saved_all_measured'])} days** sooner (at least: a home
the habit never finished counts as day {days + 1}); it was faster in
{summary['n_guided_faster']} of {summary['n_paired']} homes and slower in
{summary['n_guided_slower']}. While calibrating, the guided household followed a
median of {g['airings_followed_per_day']:.1f} prompted airings a day, thirty minutes
each, through the window it was asked to use.

Ran in {seconds:.0f} s.
"""


def main(homes: int = 24, days: int = 45, comply: float = 0.6, seed: int = 1) -> int:
    started = time.time()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scores: list[Onboarding] = []
    for i in range(homes):
        for policy in ("habit", "guided"):
            s = run_home(seed * 1000 + i, days, policy, comply)
            scores.append(s)
            print(
                f"[{i + 1:>3}/{homes}] {policy:<6} seed={s.seed} "
                f"plan={s.first_plan_day} all={s.all_measured_day} "
                f"call={s.first_call_day} followed={s.airings_followed} {s.seconds}s",
                flush=True,
            )
    summary = summarise(scores, days)
    summary["comply"] = comply
    seconds = time.time() - started
    (RESULTS_DIR / "onboarding.json").write_text(
        json.dumps(
            {"summary": summary, "seconds": round(seconds, 1), "homes": [asdict(s) for s in scores]},
            indent=1,
        ),
        encoding="utf-8",
    )
    report = markdown(summary, comply, homes, seconds)
    (RESULTS_DIR / "onboarding.md").write_text(report, encoding="utf-8")
    print()
    print(report)
    return 0


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--homes", type=int, default=24)
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--comply", type=float, default=0.6)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    raise SystemExit(main(homes=a.homes, days=a.days, comply=a.comply, seed=a.seed))
