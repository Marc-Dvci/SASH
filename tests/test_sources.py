"""Detecting an indoor emission, which is not the same as detecting its onset.

A household opens a window *while* it cooks. By the second reading the carbon
monoxide has stopped rising, because the open window is carrying it away as fast
as the hob makes it: an onset detector sees nothing and the estimator then reads
a wide-open window that looks filthy. So the detector computes the emission rate
implied by each channel's own interval balance, which is positive for as long as
the source runs, whatever the reading is doing.

The other half of the job is not flagging good airings. The best airings in a
household's record happen right after cooking, when carbon monoxide is high and
falling, and losing those would leave the fit only the airings that never
follow anything.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sash.analytics import sources
from sash.analytics.intervals import Interval


def _rows(series, *, open_frac=0.0, dt_h=0.25, activity=0.2, pm=None):
    """A run of intervals carrying a CO series, contiguous in time."""
    rows = []
    t = 1_760_000_000.0
    pm = pm or [4.0] * len(series)
    for i, co in enumerate(series):
        rows.append(
            Interval(
                t0=t,
                t1=t + dt_h * 3600.0,
                dt_h=dt_h,
                c0=pm[max(i - 1, 0)],
                c1=pm[i],
                activity=activity,
                open_frac={"w": open_frac} if open_frac else {},
                co=co,
                humidity=45.0,
                temperature=20.0,
            )
        )
        t += dt_h * 3600.0
    return rows


def test_a_flat_elevated_reading_with_a_window_open_is_flagged():
    """The case onset detection misses: a hob running against an open window."""
    rows = _rows([0.05] * 8 + [2.4] * 10, open_frac=1.0)
    sources.detect(rows)
    flagged = [iv.indoor_source for iv in rows[10:16]]
    assert any(flagged), "a source held flat by an open window must still be found"


def test_the_same_level_falling_with_everything_shut_is_not_flagged():
    """A decay is what is left after a source stops. It is not a source."""
    k = sources.CO_SEALED_LOSS_PER_H
    series = [2.4 * math.exp(-k * 0.25 * i) for i in range(14)]
    rows = _rows(series, open_frac=0.0)
    sources.detect(rows)
    assert not any(iv.indoor_source for iv in rows[2:])


def test_an_airing_straight_after_cooking_is_kept():
    """High CO, falling fast because the window is open. Nothing is emitting."""
    theta = 4.0
    k = sources.CO_SEALED_LOSS_PER_H + theta
    series = [3.0 * math.exp(-k * 0.25 * i) for i in range(10)]
    rows = _rows(series, open_frac=1.0)
    sources.detect(rows)
    assert not any(iv.indoor_source for iv in rows[1:])


def test_source_rates_are_never_negative():
    rows = _rows([2.0, 0.05, 0.02, 0.01, 0.01, 0.01, 0.01, 0.01])
    rates = sources.source_rates(rows, "co", sealed_loss=1.0, outdoor=sources.CO_OUTDOOR_PPM)
    assert (rates >= 0.0).all()


def test_a_pm_rise_with_the_home_shut_and_the_street_quiet_is_flagged():
    """Nothing in a four-channel monitor sees a candle. The particle channel can."""
    rows = _rows([0.02] * 10, open_frac=0.0, activity=0.0,
                 pm=[3, 3, 3, 3, 40, 60, 70, 70, 60, 50])
    sources.detect(rows)
    assert any(iv.indoor_source for iv in rows[4:8])


def test_sealed_loss_rate_is_measured_from_the_homes_own_quiet_pairs():
    """The CO channel's own decay is a property of the home, not a constant."""
    truth = 1.4
    series = []
    co = 3.0
    for _ in range(120):
        series.append(co)
        co = max(sources.CO_OUTDOOR_PPM, co * math.exp(-truth * 0.25))
        if co < 0.08:
            co = 3.0
    rows = _rows(series, open_frac=0.0)
    measured = sources.sealed_loss_rate(
        rows, "co", sources.CO_OUTDOOR_PPM, fallback=0.5
    )
    assert measured == pytest.approx(truth, rel=0.35)


def test_detection_on_the_simulated_home_finds_sources_without_losing_airings(
    short_rows, short_home
):
    """Precision against the simulator's own source series.

    Recall is not asserted: a candle, a toaster and a vacuum are particles with
    no combustion and no moisture, and nothing in a four-channel monitor can see
    them. What must hold is that airings are not thrown away.
    """
    _params, result = short_home
    truth = []
    for iv in short_rows:
        i0 = int(round((iv.t0 - result.t0) / 60.0))
        i1 = int(round((iv.t1 - result.t0) / 60.0))
        truth.append(bool(np.any(result.indoor_source[i0:i1] > 0.05)))

    flagged = [iv.indoor_source for iv in short_rows]
    tp = sum(1 for f, t in zip(flagged, truth) if f and t)
    fp = sum(1 for f, t in zip(flagged, truth) if f and not t)
    precision = tp / max(tp + fp, 1)
    assert precision > 0.6, f"too many clean intervals discarded (precision {precision:.2f})"

    # And the airings survive: most intervals with an opening open are kept.
    open_rows = [iv for iv in short_rows if iv.any_open]
    kept = sum(1 for iv in open_rows if not iv.indoor_source)
    assert kept > 0.5 * len(open_rows)
