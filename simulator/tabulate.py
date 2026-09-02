"""Build SASH's observation table straight from a simulated home.

``sash.analytics.intervals.build`` reads the store, which is the right thing for
the product: it proves that what Ring actually delivered is enough. The
benchmark asks a different question -- how well does the estimator recover a
household it has never seen, over many households -- and putting forty thousand
webhooks through HTTP for each of them buys nothing but minutes.

So this module produces the same :class:`Interval` rows from a ``SimResult``
directly, using the same alignment, the same episode ids and the same
camera-outage exclusion as the store path. The one thing it does not exercise is
transport, and ``tests/test_tabulate_matches_store.py`` pins the two against
each other on a short record so that stays true.
"""

from __future__ import annotations

import bisect
import datetime as dt
from typing import Sequence

from sash.analytics.intervals import SAMPLE_LAG_MIN, Interval
from sash.config import INTERVAL_MAX_MINUTES, INTERVAL_MIN_MINUTES

from .home import SimResult


def _episodes(result: SimResult) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for opening_id, spans in result.open_intervals.items():
        for a, b in spans:
            out.append((a, b, f"{opening_id}:{int(a)}"))
    out.sort()
    return out


def intervals_from_sim(
    result: SimResult, *, sample_lag_min: float = SAMPLE_LAG_MIN
) -> list[Interval]:
    """The observation table this home would have produced, without the wire."""
    times: Sequence[float] = result.vehicle_event_times
    episodes = _episodes(result)
    lag_s = sample_lag_min * 60.0
    rows: list[Interval] = []

    for prev, cur in zip(result.aqm_reports, result.aqm_reports[1:]):
        dt_min = (cur["reported_at"] - prev["reported_at"]) / 60.0
        if not (INTERVAL_MIN_MINUTES <= dt_min <= INTERVAL_MAX_MINUTES):
            continue

        w0 = prev["reported_at"] - lag_s
        w1 = cur["reported_at"] - lag_s
        span = max(w1 - w0, 1e-6)

        frac = {}
        for opening_id, spans in result.open_intervals.items():
            open_s = sum(max(0.0, min(b, w1) - max(a, w0)) for a, b in spans)
            frac[opening_id] = max(0.0, min(1.0, open_s / span))

        lo = bisect.bisect_left(times, w0)
        hi = bisect.bisect_left(times, w1)
        activity = (hi - lo) / (span / 60.0)

        camera_online = not any(a < w1 and b > w0 for a, b in result.camera_offline)

        episode_id = None
        for a, b, eid in episodes:
            if a < w1 and b > w0:
                episode_id = eid
                break

        stamp = dt.datetime.fromtimestamp(prev["reported_at"], tz=dt.timezone.utc)
        rows.append(
            Interval(
                t0=prev["reported_at"],
                t1=cur["reported_at"],
                dt_h=dt_min / 60.0,
                c0=float(prev["pm2_5"]),
                c1=float(cur["pm2_5"]),
                activity=activity,
                open_frac=frac,
                temperature=cur.get("temperature"),
                humidity=cur.get("humidity"),
                co=cur.get("co"),
                camera_online=camera_online,
                aq_fresh=True,
                hour=stamp.hour + stamp.minute / 60.0,
                weekday=stamp.weekday() < 5,
                episode_id=episode_id,
            )
        )
    return rows


def openings_of(params) -> list[tuple[str, str, str | None]]:
    """The device mapping a household would have made during setup."""
    return [(o.opening_id, o.label, o.orientation) for o in params.openings]
