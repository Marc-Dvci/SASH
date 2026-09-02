"""The refusal to name a winner, which is a shipped output.

Two openings in one flat give two different numbers even when they are the same
window twice, because the estimates are noisy and the household used them on
different days. ``simulator.home.null_home`` builds exactly that: two openings
with identical physics. SASH has to come back with "these two openings are not
yet distinguishable", and the UI has a state for that sentence.

The benchmark measures the rate across many null households. This file pins the
behaviour on one, and pins the two structural properties the test rests on: the
shuffle keeps whole airings together, and it keeps the group sizes fixed.
"""

from __future__ import annotations

import numpy as np
import pytest

from sash.analytics import estimator, nulls, sources
from simulator import home as sim
from simulator.tabulate import intervals_from_sim, openings_of


@pytest.fixture(scope="module")
def identical_openings():
    rng = np.random.default_rng(99503)
    params = sim.null_home(rng)
    result = sim.simulate(params, days=90.0, t0=1_760_000_000.0, seed=99503)
    rows = intervals_from_sim(result)
    sources.detect(rows)
    openings = openings_of(params)
    model = estimator.fit(rows, openings)
    return params, rows, openings, model


def test_two_identical_openings_are_not_declared_different(identical_openings):
    _params, rows, _openings, model = identical_openings
    if len(model.reported_openings()) < 2:
        pytest.skip("this draw did not report two openings")
    comparisons = nulls.rank_openings(rows, model, permutations=400)
    assert comparisons
    for c in comparisons:
        assert not c.significant, f"claimed a difference at p={c.p_value:.4f}"
        assert "not yet distinguishable" in c.verdict


def test_the_demo_home_is_declared_different(short_rows, short_home):
    """The other side of the same gate: a real difference has to get through."""
    params, _result = short_home
    openings = openings_of(params)
    model = estimator.fit(short_rows, openings)
    reported = model.reported_openings()
    if len(reported) < 2:
        pytest.fail("the fixture must be long enough to report two openings")
    comparisons = nulls.rank_openings(short_rows, model, permutations=200)
    assert comparisons
    gaps = [abs(c.observed_coupling_gap) for c in comparisons]
    assert max(gaps) > 1.0, "the street and courtyard windows differ by construction"


def test_fewer_than_four_airings_is_refused_rather_than_answered():
    from sash.analytics.estimator import BaselineFit
    from sash.analytics.intervals import Interval

    rows = [
        Interval(
            t0=float(i * 900),
            t1=float(i * 900 + 900),
            dt_h=0.25,
            c0=5.0,
            c1=6.0,
            activity=0.5,
            open_frac={"a": 1.0} if i % 2 else {"b": 1.0},
            episode_id=("a" if i % 2 else "b") + f":{i}",
        )
        for i in range(6)
    ]
    out = nulls.compare(
        rows,
        "a",
        "b",
        {"a": "A", "b": "B"},
        BaselineFit(loss_rate=0.5, times=np.array([0.0]), floor=np.array([3.0]), n=50),
        {"a": 3.0, "b": 3.0},
        ["a", "b"],
        permutations=50,
    )
    assert not out.significant
    assert out.p_value == 1.0
    assert "not enough separate airings" in out.verdict


def test_the_shuffle_keeps_whole_airings_together(identical_openings):
    """An airing is one draw of the weather. Splitting it would fake precision."""
    _params, rows, openings, model = identical_openings
    ids = [o[0] for o in openings]
    reported = model.reported_openings()
    if len(reported) < 2:
        pytest.skip("this draw did not report two openings")
    a = reported[0].opening_id
    blocks = nulls._episode_blocks(estimator.isolated_intervals(rows, a, ids))
    assert blocks
    for eid, ivs in blocks.items():
        assert len({iv.episode_id or eid for iv in ivs}) == 1


def test_the_shuffle_keeps_the_group_sizes_fixed(identical_openings):
    """A 40-reading opening and a 12-reading one differ in precision alone."""
    _params, rows, openings, model = identical_openings
    reported = model.reported_openings()
    if len(reported) < 2:
        pytest.skip("this draw did not report two openings")
    ids = [o[0] for o in openings]
    a, b = reported[0].opening_id, reported[1].opening_id
    n_a = len(nulls._episode_blocks(estimator.isolated_intervals(rows, a, ids)))
    n_b = len(nulls._episode_blocks(estimator.isolated_intervals(rows, b, ids)))
    out = nulls.compare(
        rows,
        a,
        b,
        {a: "A", b: "B"},
        model.baseline,
        {a: model.openings[a].exchange, b: model.openings[b].exchange},
        ids,
        permutations=20,
    )
    assert out.n_episodes_a == n_a
    assert out.n_episodes_b == n_b
