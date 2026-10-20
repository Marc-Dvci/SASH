"""When street particulate stops being the objective, and how a new home learns.

Minimising what comes in from the street is the right goal only while nothing
more pressing is going on. These pin the rules that come before the search --
a carbon-monoxide alert, an emission in the flat, high humidity, an overdue
home -- and the calibration steps a household sees before SASH can measure its
windows.
"""

from __future__ import annotations

import dataclasses

import pytest

from sash.analytics import activity as activity_mod
from sash.analytics import estimator, onboarding, planner
from simulator.tabulate import openings_of


@pytest.fixture(scope="module")
def fitted(short_rows, short_home):
    params, result = short_home
    model = estimator.fit(short_rows, openings_of(params))
    index = activity_mod.build(result.vehicle_event_times, now=short_rows[-1].t1)
    return params, result, short_rows, model, index


@pytest.fixture(scope="module")
def early(short_rows, short_home):
    """The same home three days after it was linked: nothing measured yet."""
    params, result = short_home
    cut = short_rows[0].t0 + 3 * 86400.0
    rows = [iv for iv in short_rows if iv.t1 <= cut]
    model = estimator.fit(rows, openings_of(params))
    index = activity_mod.build(
        [t for t in result.vehicle_event_times if t <= cut], now=cut
    )
    return rows, model, index


def _quiet(rows):
    """The record with nothing happening indoors in its last hour."""
    out = [dataclasses.replace(iv) for iv in rows]
    for iv in out[-6:]:
        iv.indoor_source = False
    return out


def test_a_carbon_monoxide_alert_gets_no_plan(fitted):
    _params, _result, rows, model, index = fitted
    rec = planner.recommend(
        model, index, rows, now=rows[-1].t1, current_pm=rows[-1].c1, co=12.0, explore=False
    )
    assert rec.mode == "safety"
    assert rec.best is None
    assert "12.0 ppm" in rec.reason


def test_an_emission_in_the_flat_means_air_now_not_later(fitted):
    _params, _result, rows, model, index = fitted
    rows = _quiet(rows)
    rows[-1].indoor_source = True
    now = rows[-1].t1
    rec = planner.recommend(model, index, rows, now=now, current_pm=90.0, explore=False)
    assert rec.mode == "clear_now"
    assert rec.best is not None
    assert rec.best.start_at - now <= planner.CLEAR_WITHIN_MIN * 60.0
    for plan in rec.alternatives:
        assert plan.start_at - now <= planner.CLEAR_WITHIN_MIN * 60.0
    assert rec.reason.startswith("Air now")


def test_high_humidity_means_air_now(fitted):
    _params, _result, rows, model, index = fitted
    rows = _quiet(rows)
    now = rows[-1].t1
    rec = planner.recommend(
        model, index, rows, now=now, current_pm=rows[-1].c1, humidity=78.0, explore=False
    )
    assert rec.mode == "clear_now"
    assert "78%" in rec.mode_reason


def test_an_overdue_home_is_not_moved_more_than_three_hours(fitted, monkeypatch):
    _params, _result, rows, model, index = fitted
    rows = _quiet(rows)
    now = rows[-1].t1
    real = planner.demand

    def overdue(*a, **kw):
        need = real(*a, **kw)
        need.level = "overdue"
        return need

    monkeypatch.setattr(planner, "demand", overdue)
    rec = planner.recommend(model, index, rows, now=now, current_pm=rows[-1].c1, explore=False)
    assert rec.mode == "overdue"
    for plan in [rec.best, *rec.alternatives]:
        if plan is not None:
            assert plan.start_at - now <= planner.OVERDUE_WITHIN_H * 3600.0


def test_the_rules_can_be_switched_off_for_the_benchmark(fitted):
    _params, _result, rows, model, index = fitted
    rec = planner.recommend(
        model, index, rows, now=rows[-1].t1, current_pm=rows[-1].c1, co=12.0,
        explore=False, rules=False,
    )
    assert rec.mode == "plan"
    assert rec.best is not None


def test_a_new_home_is_told_what_to_do_for_every_window(early):
    rows, model, index = early
    assert not model.reported_openings()
    steps = onboarding.steps(model, rows, index, rows[-1].t1)
    assert {s.opening_id for s in steps} == set(model.openings)
    for s in steps:
        assert 0.0 <= s.progress <= 1.0
        assert s.opening_label in s.text
        assert s.have <= s.need or not s.unit
    assert steps == sorted(steps, key=lambda s: s.progress)


def test_right_after_cooking_a_new_home_gets_a_calibration_airing(early):
    rows, model, index = early
    rows = _quiet(rows)
    rows[-1].indoor_source = True
    now = rows[-1].t1
    rec = planner.recommend(model, index, rows, now=now, current_pm=80.0, co=0.6)
    assert rec.mode == "clear_now"
    assert "cooking fumes" in rec.mode_reason
    assert rec.best is not None and rec.best.exploration
    assert rec.best.start_at == now
    assert rec.best.opening_id in {
        s.opening_id for s in rec.calibration if s.kind == "after_cooking"
    }
    # Nothing to compare and nothing to publish: it is a measurement.
    assert rec.p_better is None and rec.saving_pct is None


def test_a_candle_calls_for_air_but_not_for_a_calibration_airing(early):
    """No CO in the air means no tracer, so the airing would measure nothing."""
    rows, model, index = early
    rows = _quiet(rows)
    rows[-1].indoor_source = True
    rec = planner.recommend(
        model, index, rows, now=rows[-1].t1, current_pm=60.0, co=0.0
    )
    assert rec.mode == "clear_now"
    assert rec.best is None or not rec.best.exploration


def test_a_measured_home_has_no_calibration_steps(fitted):
    _params, _result, rows, model, index = fitted
    unreported = [o for o in model.openings.values() if not o.reported]
    steps = onboarding.steps(model, rows, index, rows[-1].t1)
    assert {s.opening_id for s in steps} == {o.opening_id for o in unreported}


def test_cooking_hours_are_read_off_the_combustion_channel(short_rows):
    hours = onboarding.cooking_hours(short_rows)
    # The demo household cooks around 12:24 and 19:36.
    assert hours, "the demo household cooks twice a day"
    assert any(abs(h - 19) <= 1 for h in hours)


def test_a_night_with_the_windows_shut_is_not_overdue(fitted):
    """Overdue is judged over a day, so an ordinary morning does not trip it."""
    _params, _result, rows, model, _index = fitted
    last = rows[-1].t1
    mornings = [last - k * 86400.0 - (last % 86400.0) + 9 * 3600.0 for k in range(1, 21)]
    levels = [
        planner.demand([iv for iv in rows if iv.t1 <= now], model, now).level
        for now in mornings
    ]
    # A twelve-hour window called 9 of these 20 mornings overdue.
    assert levels.count("overdue") <= 3
