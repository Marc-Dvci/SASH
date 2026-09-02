"""Telling an indoor emission apart from air that came in through a window.

A pan of frying onions and a bus pulling away outside produce the same thing on
the PM2.5 channel: the number goes up. Nothing in the particle reading separates
them. The other three channels of the Air Quality Monitor do.

The first version of this module looked for an emission *starting* -- a rising
CO or humidity channel. That is the right instinct and it is not enough. A
household opens a window *while* it cooks, and by the second reading the CO
channel has stopped rising: the source is still running and the open window is
carrying it away as fast as the hob makes it, so the channel sits flat at an
elevated level. An onset detector sees nothing. Measured against the simulator's
ground truth, onset detection caught 10 of the 17 contaminated intervals in the
demo home's street-window record, and the seven it missed were the worst ones:
readings where the room held at 130 ug/m3 with the window wide open, which no
ventilation model can explain and which therefore bend every parameter fitted
through them.

So this module does not look at the channel. It looks at the **source term**.

Between two readings, each channel obeys the same first-order balance the PM
channel does. Rearranged, it gives the emission rate directly:

    s = (v1 - v0*exp(-K*dt)) / M  -  K*v_out,     M = (1-exp(-K*dt))/K

with ``K`` the channel's own removal rate, which is its sealed loss plus
whatever the open window is adding. A flat elevated CO reading with the window
open is a large ``s``; the same reading with everything shut and CO falling is
``s = 0``. That distinction is the whole point, because the moment a household
most wants to open a window is right after the cooking, when CO is high and
falling, and those are the clearest airings in the record.

Against the same ground truth, the source-rate rule at ``s_co > 0.4 ppm/h``
flags 9 of the 17 with **no false positives** -- it discards no good airing. The
eight it still misses are candles, a toaster and a vacuum: particles with no
combustion and no moisture. Nothing in a four-channel monitor can see those, and
saying so is more useful than pretending otherwise. They are handled downstream
instead, by a fit whose loss is one-sided, because an indoor source can only
ever push a reading up.

Intervals flagged here are excluded from the window model. They are not deleted:
the timeline still shows them, labelled, because "that rise was your kitchen,
not your street" is one of the more useful things this product can say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .intervals import Interval

#: Sealed-home removal rate of the CO channel, per hour. Estimated from the
#: home's own quiet decays; this is the fallback before enough have been seen.
CO_SEALED_LOSS_PER_H = 0.7

#: Sealed-home removal rate of the humidity channel, per hour.
RH_SEALED_LOSS_PER_H = 0.4

#: Outdoor CO, ppm. Ambient outdoor CO in a residential street is well under
#: 0.1 ppm and the Monitor's own alert threshold is 9 ppm, so a fixed floor is
#: safe here in a way it would not be for the particle channel.
CO_OUTDOOR_PPM = 0.05

#: Reference outdoor relative humidity, %. Only the departure from it matters.
RH_OUTDOOR_PCT = 55.0

#: Air exchange assumed for an open opening while the source rate is computed.
#: The flag is not sensitive to it: an active hob is an order of magnitude clear
#: of the threshold at any plausible value.
NOMINAL_OPEN_EXCHANGE = 4.0

#: A combustion source this large, in ppm of CO per hour, means the hob is on.
CO_SOURCE_PPM_PER_H = 0.40

#: A moisture source this large, in points of RH per hour, means a pan or a
#: shower is running.
RH_SOURCE_PCT_PER_H = 25.0

#: A PM2.5 rise this fast, in ug/m3 per hour, with every monitored opening shut
#: and a quiet street, has to have come from inside.
PM_RISE_SEALED_UG_PER_H = 25.0

#: Street activity below this, in events per minute, counts as quiet.
QUIET_ACTIVITY = 0.35


@dataclass
class SourceReport:
    n_flagged: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    co_sealed_loss: float = CO_SEALED_LOSS_PER_H

    def as_dict(self) -> dict:
        return {
            "n_flagged": self.n_flagged,
            "by_reason": dict(self.by_reason),
            "co_sealed_loss": round(self.co_sealed_loss, 3),
        }


def sealed_loss_rate(
    intervals: Sequence[Interval], channel: str, outdoor: float, *, fallback: float
) -> float:
    """Removal rate of one channel with the home shut, per hour.

    Fitted on consecutive quiet pairs as a regression through the origin of the
    observed rate of change on the distance from outdoors, reweighted so that an
    emission running during one of those pairs cannot drag the rate down.
    """
    pairs: list[tuple[float, float, float]] = []
    prev: Interval | None = None
    for iv in intervals:
        v1 = getattr(iv, channel)
        v0 = getattr(prev, channel) if prev is not None else None
        if (
            prev is not None
            and abs(iv.t0 - prev.t1) < 60.0
            and not iv.any_open
            and not prev.any_open
            and v0 is not None
            and v1 is not None
        ):
            pairs.append((v0, v1, iv.dt_h))
        prev = iv
    if len(pairs) < 30:
        return fallback

    v0s = np.array([p[0] for p in pairs])
    v1s = np.array([p[1] for p in pairs])
    dt_h = np.array([p[2] for p in pairs])
    x = outdoor - v0s
    y = (v1s - v0s) / dt_h

    weights = np.ones_like(x)
    k = fallback
    for _ in range(6):
        denom = float(np.sum(weights * x * x))
        if denom < 1e-9:
            return fallback
        k = float(np.sum(weights * x * y) / denom)
        residual = y - k * x
        scale = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        if scale < 1e-9:
            break
        # An emission makes the channel fall more slowly than it should, so the
        # slow side is the contaminated side and is charged less for its error.
        z = residual / scale
        weights = np.where(residual > 0, 0.35, 0.65) * np.minimum(
            1.0, 2.5 / np.maximum(np.abs(z), 1e-9)
        )
    return float(np.clip(k, 0.1, 4.0))


def source_rates(
    intervals: Sequence[Interval],
    channel: str,
    *,
    sealed_loss: float,
    outdoor: float,
) -> np.ndarray:
    """Emission rate implied by one channel, per interval, per hour.

    Non-negative by construction: a channel falling faster than its removal rate
    explains means the removal rate is a little off, not that the home is
    absorbing carbon monoxide.
    """
    out = np.zeros(len(intervals))
    prev: Interval | None = None
    for i, iv in enumerate(intervals):
        v1 = getattr(iv, channel)
        v0 = getattr(prev, channel) if prev is not None else None
        if (
            prev is not None
            and abs(iv.t0 - prev.t1) < 60.0
            and v0 is not None
            and v1 is not None
        ):
            k = sealed_loss + sum(iv.open_frac.values()) * NOMINAL_OPEN_EXCHANGE
            decay = float(np.exp(-min(k * iv.dt_h, 40.0)))
            m = (1.0 - decay) / max(k, 1e-9)
            out[i] = max(0.0, (v1 - v0 * decay) / max(m, 1e-9) - k * outdoor)
        prev = iv
    return out


def detect(intervals: Sequence[Interval]) -> SourceReport:
    """Mark intervals during which an indoor emission was running.

    Mutates ``Interval.indoor_source`` and ``Interval.source_rate`` and returns
    a summary.
    """
    report = SourceReport()
    if len(intervals) < 2:
        return report

    co_loss = sealed_loss_rate(
        intervals, "co", CO_OUTDOOR_PPM, fallback=CO_SEALED_LOSS_PER_H
    )
    report.co_sealed_loss = co_loss
    s_co = source_rates(intervals, "co", sealed_loss=co_loss, outdoor=CO_OUTDOOR_PPM)
    s_rh = source_rates(
        intervals, "humidity", sealed_loss=RH_SEALED_LOSS_PER_H, outdoor=RH_OUTDOOR_PCT
    )

    for i, iv in enumerate(intervals):
        iv.source_rate = float(s_co[i])
        reason = None
        if s_co[i] > CO_SOURCE_PPM_PER_H:
            reason = "combustion (carbon monoxide being produced)"
        elif s_rh[i] > RH_SOURCE_PCT_PER_H:
            reason = "moisture (a pan or a shower running)"
        elif (
            not iv.any_open
            and iv.camera_online
            and iv.activity < QUIET_ACTIVITY
            and iv.rate > PM_RISE_SEALED_UG_PER_H
        ):
            reason = "a rise with everything shut and the street quiet"

        iv.indoor_source = reason is not None
        if reason:
            report.n_flagged += 1
            report.by_reason[reason] = report.by_reason.get(reason, 0) + 1

    return report
