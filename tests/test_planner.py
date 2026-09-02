"""The plan, and the rules it is not allowed to break.

The load-bearing one is the target-air invariant. SASH moves *when* a household
airs and *which* opening it uses. It never moves *how much* air the home gets,
because "open it for less time" is not ventilation advice, it is a way of
scoring well on a particulate metric by under-ventilating a flat. So the
recommended duration times the opening's exchange rate has to come out at the
household's own historical median, whichever opening is chosen.

The rest guard the ledger: a plan the household ignored is not evidence about
the forecast, and a plan whose window was contaminated is not scored at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from sash.analytics import activity as activity_mod
from sash.analytics import estimator, holdout, planner, sources
from sash.analytics.intervals import Interval
from simulator import home as sim
from simulator.tabulate import intervals_from_sim, openings_of


@pytest.fixture(scope="module")
def fitted(short_rows, short_home):
    params, result = short_home
    openings = openings_of(params)
    model = estimator.fit(short_rows, openings)
    index = activity_mod.build(result.vehicle_event_times, now=short_rows[-1].t1)
    return params, result, short_rows, model, index


def test_the_recommended_duration_delivers_the_target_air_whichever_opening(fitted):
    """The target-air invariant: duration x exchange = the household's median."""
    _params, _result, rows, model, index = fitted
    if not model.reported_openings():
        pytest.fail("the fixture must be long enough to report an opening")
    now = rows[-1].t1
    rec = planner.recommend(
        model, index, rows, now=now, current_pm=rows[-1].c1, explore=False
    )
    if rec.best is None:
        pytest.skip("no plan offered in this window")
    target = rec.demand.target_exchanges
    for plan in [rec.best, *rec.alternatives]:
        est = model.openings[plan.opening_id]
        delivered = est.exchange * plan.duration_min / 60.0
        if planner.MIN_DURATION_MIN < plan.duration_min < planner.MAX_DURATION_MIN:
            assert delivered == pytest.approx(target, rel=0.02), (
                f"{plan.opening_label} delivers {delivered:.2f} against a target of {target:.2f}"
            )


def test_a_plan_is_never_offered_at_an_hour_the_household_never_airs(fitted):
    """Four in the morning is the quietest street of the week and nobody is up."""
    _params, _result, rows, model, index = fitted
    if not model.reported_openings():
        pytest.fail("the fixture must be long enough to report an opening")
    hours = planner.airing_hours(rows)
    rec = planner.recommend(
        model, index, rows, now=rows[-1].t1, current_pm=rows[-1].c1, explore=False
    )
    for plan in [rec.best, *rec.alternatives]:
        if plan is None:
            continue
        hour = (plan.start_at % 86400.0) / 3600.0
        assert planner.within_habit(hour, hours)


def test_within_habit_needs_repeated_support_not_one_occasion():
    hours = np.array([8.0, 8.2, 8.1, 19.0])
    assert planner.within_habit(8.1, hours)
    assert not planner.within_habit(19.0, hours), "one airing at seven is not a habit"
    assert not planner.within_habit(4.0, hours)


def test_an_empty_history_offers_every_hour():
    assert planner.within_habit(4.0, np.array([]))


# --- verification -----------------------------------------------------------


def _row(t0, dt_h, c0, c1, *, opens=None, source=False, co=0.05, humidity=45.0):
    return Interval(
        t0=t0,
        t1=t0 + dt_h * 3600.0,
        dt_h=dt_h,
        c0=c0,
        c1=c1,
        activity=0.4,
        open_frac=opens or {},
        co=co,
        humidity=humidity,
        temperature=20.0,
        indoor_source=source,
    )


def _quiet_rows(t0, n, level=4.0):
    return [_row(t0 + i * 900.0, 0.25, level, level) for i in range(n)]


def _model():
    m = estimator.HomeModel()
    m.fitted = True
    m.baseline = estimator.BaselineFit(
        loss_rate=0.7, times=np.array([0.0]), floor=np.array([4.0]), n=100
    )
    m.openings = {
        "w": estimator.OpeningEstimate("w", "Window", "street", exchange=4.0, coupling=5.0)
    }
    m.openings["w"].reported = True
    return m


def test_a_plan_the_household_ignored_is_recorded_and_not_scored():
    t0 = 1_760_000_000.0
    rows = _quiet_rows(t0 - 8 * 3600.0, 40) + _quiet_rows(t0, 20)
    plan = {
        "plan_id": "p",
        "opening_id": "w",
        "opening_label": "Window",
        "start_at": t0 + 900.0,
        "duration_min": 45.0,
        "pred_intake": 300.0,
        "pred_intake_lo": 0.0,
        "pred_intake_hi": 900.0,
    }
    out = planner.verify(_model(), rows, plan)
    assert out.verdict == "not_followed"
    assert out.realised_intake == 0.0


def test_an_indoor_emission_during_the_airing_makes_it_unverifiable():
    t0 = 1_760_000_000.0
    rows = _quiet_rows(t0 - 8 * 3600.0, 40)
    rows += [
        _row(t0, 0.25, 4.0, 20.0, opens={"w": 1.0}),
        _row(t0 + 900, 0.25, 20.0, 60.0, opens={"w": 1.0}, source=True),
        _row(t0 + 1800, 0.25, 60.0, 40.0, opens={"w": 1.0}),
    ]
    rows += _quiet_rows(t0 + 2700, 20)
    plan = {
        "plan_id": "p",
        "opening_id": "w",
        "opening_label": "Window",
        "start_at": t0,
        "duration_min": 45.0,
        "pred_intake": 300.0,
        "pred_intake_lo": 0.0,
        "pred_intake_hi": 900.0,
    }
    out = planner.verify(_model(), rows, plan)
    assert out.verdict == "unverifiable"


def test_cooking_after_the_airing_truncates_the_tail_and_does_not_void_the_airing():
    """A household that airs at eleven and cooks at one has a measurable airing.

    The recovery tail exists to catch load that is still in the room after the
    window shuts. When something else raises the readings during it, the tail is
    cut there and completed at the home's own measured decay rate, which is what
    the forecast assumed the room would do anyway.
    """
    t0 = 1_760_000_000.0
    rows = _quiet_rows(t0 - 8 * 3600.0, 40)
    rows += [
        _row(t0, 0.25, 4.0, 12.0, opens={"w": 1.0}),
        _row(t0 + 900, 0.25, 12.0, 18.0, opens={"w": 1.0}),
        _row(t0 + 1800, 0.25, 18.0, 20.0, opens={"w": 1.0}),
        _row(t0 + 2700, 0.25, 20.0, 15.0),
        _row(t0 + 3600, 0.25, 15.0, 12.0),
        # An hour later, a hob.
        _row(t0 + 4500, 0.25, 12.0, 90.0, source=True),
        _row(t0 + 5400, 0.25, 90.0, 140.0, source=True),
    ]
    rows += _quiet_rows(t0 + 6300, 40)
    plan = {
        "plan_id": "p",
        "opening_id": "w",
        "opening_label": "Window",
        "start_at": t0,
        "duration_min": 45.0,
        "pred_intake": 900.0,
        "pred_intake_lo": 0.0,
        "pred_intake_hi": 2400.0,
    }
    out = planner.verify(_model(), rows, plan)
    assert out.verdict in ("inside_interval", "above", "below")
    assert out.realised_intake > 0.0


def test_a_second_opening_during_the_airing_makes_it_unverifiable():
    t0 = 1_760_000_000.0
    rows = _quiet_rows(t0 - 8 * 3600.0, 40)
    rows += [
        _row(t0, 0.25, 4.0, 12.0, opens={"w": 1.0, "other": 1.0}),
        _row(t0 + 900, 0.25, 12.0, 18.0, opens={"w": 1.0, "other": 1.0}),
        _row(t0 + 1800, 0.25, 18.0, 14.0, opens={"w": 1.0}),
    ]
    rows += _quiet_rows(t0 + 2700, 30)
    plan = {
        "plan_id": "p",
        "opening_id": "w",
        "opening_label": "Window",
        "start_at": t0,
        "duration_min": 45.0,
        "pred_intake": 300.0,
        "pred_intake_lo": 0.0,
        "pred_intake_hi": 900.0,
    }
    assert planner.verify(_model(), rows, plan).verdict == "unverifiable"


def test_followed_fraction_measures_the_named_opening_only():
    t0 = 1_760_000_000.0
    rows = [
        _row(t0, 0.25, 4.0, 4.0, opens={"w": 1.0}),
        _row(t0 + 900, 0.25, 4.0, 4.0, opens={"other": 1.0}),
    ]
    assert planner.followed_fraction(rows, "w", t0, t0 + 1800.0) == pytest.approx(0.5)


def test_interval_scale_grows_with_the_air_in_play_and_the_duration():
    small = planner.interval_scale(5.0, 5.0, 20.0)
    longer = planner.interval_scale(5.0, 5.0, 60.0)
    dirtier = planner.interval_scale(25.0, 25.0, 20.0)
    assert longer > small and dirtier > small


# --- the held-out replay ----------------------------------------------------


def test_the_backtest_holds_out_what_it_scores(short_rows, short_home):
    params, _result = short_home
    back = holdout.run(short_rows, openings_of(params))
    if back.n_scored == 0:
        pytest.fail("the fixture must be long enough to score a held-out slice")
    assert back.split_at > 0
    for airing in back.airings:
        assert airing.start_at >= back.split_at


def test_the_calibration_multiplier_is_a_quantile_of_standardised_errors():
    priced = [
        holdout.Priced("w", "W", 0.0, 30.0, pred=100.0, half_width=10.0, realised=100.0 + z)
        for z in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20)
    ]
    cal = holdout._multiplier(priced, 0.90)
    assert cal.fitted
    assert cal.k == pytest.approx(1.8, abs=0.3)
