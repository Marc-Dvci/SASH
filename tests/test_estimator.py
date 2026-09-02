"""The estimator, against arithmetic that does not depend on the estimator.

Each test here pins one of the three ideas the model rests on:

* an interval is not a derivative -- the response of a first-order system over
  fifteen minutes is an integral, and comparing candidates on the reading they
  predict is the only way their residuals share a scale;
* the tracer has no street in it -- outdoor carbon monoxide is negligible next
  to indoor combustion, so the decay constant of the CO channel measures the
  opening and nothing else;
* an undetected indoor emission can only push a reading up, so the loss that
  fits the particle channel has to be one-sided.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sash.analytics import estimator
from sash.analytics.intervals import Interval


def test_response_matches_a_numerical_integration():
    """``response`` is the exact solution; check it against Euler with tiny steps."""
    for k in (0.4, 1.5, 6.0):
        for dt_h in (0.25, 0.5):
            decay, gain = estimator.response(np.array([k]), np.array([dt_h]))
            # dc/dt = -k c + 1, from c = 0.
            c = 0.0
            steps = 20000
            h = dt_h / steps
            for _ in range(steps):
                c += h * (1.0 - k * c)
            assert float(gain[0]) == pytest.approx(c, rel=2e-3)
            assert float(decay[0]) == pytest.approx(math.exp(-k * dt_h), rel=1e-9)


def test_attenuation_falls_away_as_the_interval_outruns_the_dynamics():
    """Near one when the interval is short, near zero when it is long.

    The exponent is clamped at 40 so an enormous ``K dt`` cannot overflow, which
    puts a floor of 1/40 on the returned value. That is the clamp, not the
    identity, and it only ever bites where the answer is already zero.
    """
    short = estimator.attenuation(np.array([2.0]), np.array([1e-6]))
    mid = estimator.attenuation(np.array([2.0]), np.array([0.25]))
    long = estimator.attenuation(np.array([2.0]), np.array([50.0]))
    assert float(short[0]) == pytest.approx(1.0, abs=1e-4)
    assert 0.7 < float(mid[0]) < 0.85
    assert float(long[0]) < 0.03


def test_nnls_ridge_never_returns_a_negative_coefficient():
    rng = np.random.default_rng(0)
    A = rng.normal(size=(60, 3))
    # A target that a free least squares would fit with a negative second term.
    y = A[:, 0] * 2.0 - A[:, 1] * 5.0 + rng.normal(scale=0.1, size=60)
    x = estimator.nnls_ridge(A, y, alpha=1e-6)
    assert (x >= -1e-12).all()


def test_onesided_nnls_forgives_a_reading_above_the_fit_and_not_below_it():
    """An undetected candle raises a reading. Nothing lowers one.

    So the loss has to redescend above the fit and stay quadratic below it, and
    the test is that moving one point up moves the fit less than moving the same
    point down by the same amount.
    """
    rng = np.random.default_rng(3)
    A = np.column_stack([np.ones(60), rng.uniform(0.0, 1.0, 60)])
    truth = np.array([1.0, 2.0])
    # Real noise, so the robust scale the loss is measured in is a real number.
    # On a noiseless fit every residual is astronomically many scales out and
    # both sides are rejected outright, which measures nothing.
    y = A @ truth + rng.normal(scale=0.30, size=60)

    base = estimator.onesided_nnls(A, y)[0]
    pulled_up, pulled_down = [], []
    for i in range(60):
        up, down = y.copy(), y.copy()
        up[i] += 1.5
        down[i] -= 1.5
        pulled_up.append(np.linalg.norm(estimator.onesided_nnls(A, up)[0] - base))
        pulled_down.append(np.linalg.norm(estimator.onesided_nnls(A, down)[0] - base))

    assert float(np.mean(pulled_up)) < float(np.mean(pulled_down)), (
        "a reading above the fit must move it less than the same reading below it"
    )


def test_the_weights_are_lower_above_the_fit_than_below_it():
    """The mechanism behind the test above, stated directly."""
    rng = np.random.default_rng(11)
    A = np.column_stack([np.ones(80), rng.uniform(0.0, 1.0, 80)])
    y = A @ np.array([1.0, 2.0]) + rng.normal(scale=0.30, size=80)
    _beta, residual, weights = estimator.onesided_nnls(A, y)
    above = weights[residual > 0]
    below = weights[residual < 0]
    assert above.size and below.size
    assert float(np.mean(above)) < float(np.mean(below))


def _decay_rows(exchange: float, sealed: float, fracs, *, co0=6.0, dt_h=0.25):
    """Synthetic CO decays with a known exchange rate and no source."""
    rows = []
    t = 1_760_000_000.0
    for frac in fracs:
        k = sealed + exchange * frac
        c1 = co0 * math.exp(-k * dt_h)
        rows.append(
            Interval(
                t0=t,
                t1=t + dt_h * 3600.0,
                dt_h=dt_h,
                c0=4.0,
                c1=4.0,
                activity=0.2,
                open_frac={"w": frac},
                co=c1,
                humidity=45.0,
                temperature=20.0,
            )
        )
        rows[-1].co0 = co0  # noqa: B010 - only read by this test's helper
        t += 3600.0
    return rows


def test_fit_tracer_recovers_a_known_slope():
    """The regression's slope is the exchange rate; its intercept is the home."""
    sealed, exchange = 0.9, 4.5
    fracs = np.linspace(0.0, 1.0, 40)
    rates = sealed + exchange * fracs
    fit = estimator.fit_tracer(fracs, rates)
    assert fit.exchange == pytest.approx(exchange, rel=0.05)
    assert fit.sealed_loss == pytest.approx(sealed, abs=0.2)


def test_fit_tracer_follows_the_upper_envelope():
    """A source can only make a decay look slower, so outliers are one-sided.

    Half the observations are contaminated downward. The fit must stay near the
    clean line rather than splitting the difference.
    """
    sealed, exchange = 0.9, 4.5
    fracs = np.linspace(0.0, 1.0, 60)
    rates = sealed + exchange * fracs
    contaminated = rates.copy()
    contaminated[::2] -= 2.5
    fit = estimator.fit_tracer(fracs, contaminated)
    clean = estimator.fit_tracer(fracs, rates)
    assert abs(fit.exchange - clean.exchange) < 0.5 * exchange


def test_the_model_reproduces_a_simulated_homes_open_window_readings(short_home, short_rows):
    """The oracle check: true parameters into the identity, against the readings.

    If this fails the physics is wrong and no amount of fitting will save it.
    """
    params, result = short_home
    lam = {o.opening_id: o.lam for o in params.openings}
    g = {o.opening_id: o.g for o in params.openings}
    k_sealed = params.lam_base + params.deposition

    residuals = []
    for iv in short_rows:
        if iv.indoor_source or not iv.camera_online:
            continue
        open_ids = iv.open_openings()
        if len(open_ids) != 1:
            continue
        oid = open_ids[0]
        frac = iv.open_frac[oid]
        k = k_sealed + lam[oid] * frac
        decay, gain = estimator.response(np.array([k]), np.array([iv.dt_h]))
        # Outdoor concentration is not observable; take the simulator's own.
        i = int(round((iv.t0 - result.t0) / 60.0))
        background = float(np.mean(result.background[i : i + int(iv.dt_h * 60)]))
        driving = (
            params.lam_base * params.penetration * background
            + lam[oid] * frac * (background + g[oid] * iv.activity)
        )
        predicted = iv.c0 * float(decay[0]) + float(gain[0]) * driving
        residuals.append(predicted - iv.c1)

    assert len(residuals) >= 20
    rms = float(np.sqrt(np.mean(np.square(residuals))))
    # The bound is loose because two approximations sit inside this check that
    # the model itself does not make: the regional background is averaged over
    # the interval rather than integrated through it, and partly-open intervals
    # are included at their open fraction. A real break in the mass balance
    # takes this to hundreds, not to single figures.
    assert rms < 12.0, f"the mass balance does not reproduce the readings (rms {rms:.2f})"


def test_the_fit_recovers_the_demo_homes_ranking(short_rows, short_home):
    params, _result = short_home
    openings = [(o.opening_id, o.label, o.orientation) for o in params.openings]
    model = estimator.fit(short_rows, openings, bootstrap=0)
    assert model.fitted
    reported = model.reported_openings()
    if len(reported) < 2:
        pytest.fail("the fixture must be long enough to report two openings")
    truth = {o.opening_id: o.g for o in params.openings}
    best = min(reported, key=lambda o: o.coupling)
    assert best.opening_id == min(reported, key=lambda o: truth[o.opening_id]).opening_id


def test_an_opening_with_no_airings_is_not_given_a_number(short_rows, short_home):
    """A product that will not answer has to say what it is waiting for."""
    params, _ = short_home
    openings = [(o.opening_id, o.label, o.orientation) for o in params.openings]
    openings.append(("never_used", "Loft hatch", "garden"))
    model = estimator.fit(short_rows, openings, bootstrap=0)
    est = model.openings["never_used"]
    assert not est.reported
    assert est.reason and est.reason != "no data yet"
