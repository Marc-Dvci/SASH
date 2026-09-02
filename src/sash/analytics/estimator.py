"""The household model: what each opening delivers, and what it costs.

The physics is an ordinary single-zone mass balance. Between two PM2.5 readings,

    dC/dt = o_w * theta_w * (D_w - C)  +  b * (Cstar(t) - C)  +  s(t)

    C          indoor PM2.5, ug/m3
    b          loss rate of a sealed home, per hour (leakage + deposition)
    Cstar(t)   the level a sealed home settles at, measured not assumed
    theta_w    air exchange rate through opening w, per hour  -- the benefit
    D_w        concentration of the air arriving through w,
               D_w = B_w + g_w * A(t)
    B_w        what the air outside that opening carries on a quiet street
    g_w        street coupling, ug/m3 per vehicle-motion event per minute
    A(t)       street activity, events per minute
    s(t)       indoor emission, one-sided and non-negative

Four things about the Ring data decide whether any of this is recoverable, and
each is a design commitment rather than a preference.


**An interval is not a derivative.**

The Air Quality Monitor reports about every fifteen minutes and an open window
drives the removal rate to four or six per hour, so the room is most of the way
to its new level before the next reading arrives. A fit on the raw slope
underestimates every rate by a factor that depends on the rate itself.

The first version of this module corrected the slope by that factor and fitted
the corrected slope. The correction is exact and the fit built on it still
failed, for a reason worth recording: dividing by the attenuation factor
multiplies the residuals by its reciprocal, so a candidate exchange rate twice
as large arrives with residuals twice as large, and a search that scores
candidates on those residuals walks down to the smallest rate on the grid every
time. On the demo home that search returned 0.15, 0.41 and 0.12 per hour against
truths of 4.2, 3.1 and 6.0.

So nothing is deconvolved. The linear ODE is integrated exactly across the
interval and the model is compared with the reading that actually arrived:

    C1 = C0*exp(-K*dt) + (1-exp(-K*dt))/K * (b*Cstar + o*theta*D)

Residuals are then in ug/m3 of measured particulate, on one scale, comparable
across every candidate. With the true parameters substituted in, that identity
reproduces the demo home's uncontaminated open-window readings to an RMS error
of 1.45 ug/m3, about the Monitor's own noise. The model is right; everything
below is about estimating it from a household's ordinary week.


**The exchange rate is measured on a channel with no street in it.**

The particle channel carries two unknowns at once: how fast the opening moves
air, and how dirty the air it moves is. They trade off, and on fifteen-minute
readings they trade off badly.

Carbon monoxide does not have that problem. Outdoor CO in a residential street
is negligible next to indoor combustion, so between cooking events the CO
channel is a decaying tracer with no source and no street term, and its decay
constant is exactly the quantity wanted:

    K_co = k_co + o_w * theta_w

Regress the observed decay constant on the open fraction across the whole record
and the slope is the opening's air exchange rate, with the home's own sealed CO
loss falling out as the intercept. This is tracer-gas decay, the standard way
air exchange is measured in a building, and Ring ships the tracer.

The regression follows the upper envelope, because an indoor source can only
make a decay look slower than it is.

Over five sixty-day seeds of the demo home the tracer returns 4.35 / 3.76 /
3.66 / 4.11 / 3.64 against a truth of 4.2 for the street window, and 7.17 /
4.00 / 6.04 / 5.21 / 6.09 against 6.0 for the balcony door. The ordering of the
three openings by exchange rate is right in every seed.


**The background moves, so measure it.**

``Cstar(t)`` is a rolling low quantile of the home's own readings: the floor the
flat returns to when nothing is happening. A constant intercept turns a week of
dirty weather into a property of whichever window happened to be open that week.


**Indoor emissions are one-sided.**

Cooking only ever pushes a reading up. Intervals with a detectable source are
dropped (see ``sources.py``), but a toaster leaves no trace on any channel the
Monitor has, so the fit that survives it is one-sided as well: residuals above
the fit are charged less, and far above it almost nothing, because that is where
the toaster lives.

Openings are fitted one at a time. Two windows open together are one ventilation
event, not two, and a joint fit hands one window's behaviour to its neighbour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..config import MIN_INTERVALS_PER_OPENING
from .intervals import Interval

#: Minimum spread of street activity, events per minute, seen while an opening
#: was open, before its street coupling is reported at all.
MIN_ACTIVITY_SPREAD = 0.18

#: Minimum spread of starting indoor concentration, ug/m3.
MIN_C0_SPREAD = 2.5

#: Minimum number of distinct open episodes per opening.
MIN_EPISODES = 5

#: Minimum number of tracer decays observed with the opening open. Below this
#: the exchange rate is a guess dressed as a measurement.
MIN_TRACER_ROWS = 6

#: Outdoor carbon monoxide, ppm.
CO_OUTDOOR_PPM = 0.05

#: A CO reading has to sit this far above outdoors to start a usable decay.
CO_TRACER_FLOOR_PPM = 0.12

#: Asymmetry of the particle fit. Below 0.5 the fit follows the lower envelope,
#: which is where the air is, rather than the mean, which is where the cooking
#: is.
TAU_PM = 0.40

#: Asymmetry of the sealed-home fit. Barely below 0.5: a quiet interval that
#: rose did so because of an emission nothing detected, not because the envelope
#: leaks upward, but quiet intervals are the cleanest data in the record and
#: charging them much less on one side throws that away.
TAU_BASELINE = 0.42

#: Asymmetry of the tracer regression. Above 0.5 it follows the fast side of the
#: decays, which is the side an indoor source cannot manufacture.
TAU_TRACER = 0.75

#: Residuals beyond this many robust deviations are discounted, so one bad
#: reading cannot set a window's reputation. Above the fit the discount is
#: quadratic and effectively removes the point, because that is the side an
#: undetected indoor source lives on.
OUTLIER_CAP = 2.2

#: Kept so the sensitivity sweep in bench/ can report that there is no longer
#: anything to sweep: the background outside each opening is estimated now, not
#: assumed as a multiple of the floor.
DEFAULT_RHO = 1.0


def attenuation(k: np.ndarray | float, dt_h: np.ndarray | float) -> np.ndarray:
    """``(1 - exp(-K*dt)) / (K*dt)``: observed slope over instantaneous rate.

    Kept because it is the clearest statement of why a fifteen-minute cadence
    cannot be treated as a derivative, and because ``tests/test_estimator.py``
    asserts the identity against a numerical integration.
    """
    x = np.clip(np.asarray(k, dtype=float) * np.asarray(dt_h, dtype=float), 0.0, 40.0)
    out = np.ones_like(x, dtype=float)
    big = x > 1e-6
    out[big] = (1.0 - np.exp(-x[big])) / x[big]
    return np.clip(out, 1e-3, 1.0)


def response(k: np.ndarray, dt_h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(exp(-K*dt), (1-exp(-K*dt))/K)``.

    The first is how much of the old level the room still remembers at the next
    reading; the second is the weight that reading gives to whatever was driving
    the room in between.
    """
    decay = np.exp(-np.clip(k * dt_h, 0.0, 40.0))
    return decay, (1.0 - decay) / np.maximum(k, 1e-9)


# --- numerics ---------------------------------------------------------------


def nnls_ridge(
    X: np.ndarray,
    y: np.ndarray,
    *,
    weights: np.ndarray | None = None,
    alpha: float = 1e-3,
    iterations: int = 200,
    tol: float = 1e-9,
) -> np.ndarray:
    """Non-negative ridge regression by coordinate descent.

    Non-negativity is physics, not regularisation: an opening cannot pull
    particles out of the air, and traffic cannot make the air arriving through a
    window cleaner. Ridge is relative to each column's own scale, so a column in
    ug/m3 is not penalised harder than one in events per minute.
    """
    n, p = X.shape
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    beta = np.zeros(p)
    Xw = X * w[:, None]
    denom = np.einsum("ij,ij->j", Xw, X) * (1.0 + alpha)
    residual = y - X @ beta
    for _ in range(iterations):
        delta_max = 0.0
        for j in range(p):
            if denom[j] <= 1e-12:
                continue
            partial = residual + X[:, j] * beta[j]
            candidate = max(0.0, float(np.dot(Xw[:, j], partial) / denom[j]))
            change = candidate - beta[j]
            if change != 0.0:
                residual = partial - X[:, j] * candidate
                beta[j] = candidate
                delta_max = max(delta_max, abs(change))
        if delta_max < tol:
            break
    return beta


def onesided_nnls(
    X: np.ndarray,
    y: np.ndarray,
    *,
    tau: float = TAU_PM,
    cap: float = OUTLIER_CAP,
    alpha: float = 1e-3,
    rounds: int = 6,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Non-negative regression with a loss that knows which way cooking pushes.

    Below the fit the loss is nearly quadratic and an outlier is merely capped:
    a reading that low is a reading, and it should count. Above the fit the loss
    redescends, so a reading far above is discounted towards nothing. The
    asymmetry is not robustness for its own sake. An undetected indoor emission
    can only put a point above the fit, never below it, so the two sides are
    genuinely different populations.

    Returns ``(coefficients, residuals, weights)``.
    """
    beta = nnls_ridge(X, y, alpha=alpha)
    residual = y - X @ beta
    weights = np.ones_like(y)
    for _ in range(rounds):
        scale = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        scale = max(scale, 1e-6)
        z = residual / scale
        weights = np.where(residual > 0, tau, 1.0 - tau)
        far = np.abs(z) > cap
        weights = np.where(
            far & (z > 0), tau * (cap / np.maximum(np.abs(z), 1e-9)) ** 2, weights
        )
        weights = np.where(
            far & (z < 0), (1.0 - tau) * cap / np.maximum(np.abs(z), 1e-9), weights
        )
        beta = nnls_ridge(X, y, weights=weights, alpha=alpha)
        residual = y - X @ beta
    return beta, residual, weights


def rolling_quantile(
    values: np.ndarray, times: np.ndarray, *, window_h: float, q: float, smooth: int = 5
) -> np.ndarray:
    """Centred rolling quantile over a time window, then a median smooth."""
    n = len(values)
    if n == 0:
        return np.array([])
    out = np.zeros(n)
    half = window_h * 3600.0
    lo = hi = 0
    for i in range(n):
        while times[lo] < times[i] - half:
            lo += 1
        while hi < n and times[hi] <= times[i] + half:
            hi += 1
        out[i] = float(np.percentile(values[lo:hi], q))
    if smooth > 0 and n > 2 * smooth:
        padded = np.r_[out[:smooth], out, out[-smooth:]]
        out = np.array(
            [float(np.median(padded[j : j + 2 * smooth + 1])) for j in range(n)]
        )
    return out


def floor_at(
    intervals: Sequence[Interval],
    when: float,
    *,
    window_h: float = 4.0,
    q: float = 15.0,
    minimum: int = 6,
) -> float | None:
    """The sealed-home floor just before ``when``, on the same definition the
    baseline fit uses.

    Backwards only, so it is a quantity a forecast made at ``when`` could have
    had. :func:`rolling_quantile` is centred and cannot be, which is the whole
    difference between fitting and forecasting.
    """
    for widen in (1.0, 3.0, 12.0):
        lo = when - window_h * widen * 3600.0
        values = [iv.c0 for iv in intervals if lo <= iv.t0 <= when]
        if len(values) >= minimum:
            return float(np.percentile(values, q))
    return None


# --- stage 1: the sealed home ----------------------------------------------


@dataclass
class BaselineFit:
    """The home with everything shut."""

    loss_rate: float = 0.0  # b, per hour
    times: np.ndarray = field(default_factory=lambda: np.array([]))
    floor: np.ndarray = field(default_factory=lambda: np.array([]))  # Cstar(t)
    n: int = 0

    def c_star(self, t: float) -> float:
        if self.times.size == 0:
            return 0.0
        idx = int(np.searchsorted(self.times, t))
        idx = min(max(idx, 0), self.times.size - 1)
        return float(self.floor[idx])

    @property
    def recent_floor(self) -> float:
        return float(self.floor[-1]) if self.floor.size else 0.0

    @property
    def half_life_min(self) -> float:
        return 60.0 * math.log(2.0) / self.loss_rate if self.loss_rate > 1e-6 else 0.0


def fit_baseline(
    intervals: Sequence[Interval], *, window_h: float = 4.0, q: float = 15.0
) -> BaselineFit:
    """Measure the sealed-home floor and the rate the home returns to it.

    The floor is a rolling low quantile of the home's own readings. The rate is
    profiled on the exact interval solution across quiet intervals, so it needs
    no attenuation correction and no fixed-point iteration.
    """
    if not intervals:
        return BaselineFit()
    times = np.array([iv.t0 for iv in intervals])
    c0_all = np.array([iv.c0 for iv in intervals])
    floor = rolling_quantile(c0_all, times, window_h=window_h, q=q)

    quiet = [
        i
        for i, iv in enumerate(intervals)
        if iv.usable and not iv.any_open and not iv.indoor_source
    ]
    fit = BaselineFit(times=times, floor=floor, n=len(quiet))
    if len(quiet) < 8:
        return fit

    idx = np.array(quiet)
    c0 = c0_all[idx]
    c1 = np.array([intervals[i].c1 for i in quiet])
    dt_h = np.array([intervals[i].dt_h for i in quiet])
    star = floor[idx]

    best_loss, best_b = np.inf, 0.6
    for b in np.arange(0.10, 2.51, 0.02):
        decay, weight = response(np.full_like(dt_h, b), dt_h)
        residual = c1 - (c0 * decay + weight * b * star)
        scale = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        scale = max(scale, 1e-6)
        z = np.abs(residual) / scale
        # One-sided here too: a quiet interval that rose did so because of an
        # emission nothing detected, not because the envelope leaks upward.
        w = np.where(residual > 0, TAU_BASELINE, 1.0 - TAU_BASELINE) * np.minimum(
            1.0, OUTLIER_CAP / np.maximum(z, 1e-9)
        )
        loss = float(np.sum(w * residual * residual))
        if loss < best_loss:
            best_loss, best_b = loss, float(b)
    fit.loss_rate = best_b
    return fit


# --- stage 2: the exchange rate, from the tracer channel ---------------------


@dataclass
class TracerFit:
    """Air exchange measured by carbon-monoxide decay."""

    sealed_loss: float = 0.0  # k_co, per hour
    exchange: float = 0.0  # theta_w, per hour
    n_open: int = 0
    n_total: int = 0

    def as_dict(self) -> dict:
        return {
            "sealed_loss": round(self.sealed_loss, 3),
            "exchange": round(self.exchange, 3),
            "n_open": self.n_open,
            "n_total": self.n_total,
        }


def tracer_decays(
    intervals: Sequence[Interval], opening_id: str, all_ids: Sequence[str]
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Observed CO decay constants against the open fraction of one opening.

    A pair of consecutive readings contributes when the CO channel was clearly
    above outdoor level and falling. Intervals during which any *other* opening
    was open are excluded, because the decay they show belongs to that one.
    """
    others = [o for o in all_ids if o != opening_id]
    fracs: list[float] = []
    rates: list[float] = []
    groups: list[str] = []
    prev: Interval | None = None
    for iv in intervals:
        co1 = iv.co
        co0 = prev.co if prev is not None else None
        if (
            prev is not None
            and abs(iv.t0 - prev.t1) < 60.0
            and co0 is not None
            and co1 is not None
            and iv.dt_h > 0
            and all(iv.open_frac.get(o, 0.0) < 0.10 for o in others)
        ):
            above0 = co0 - CO_OUTDOOR_PPM
            above1 = co1 - CO_OUTDOOR_PPM
            if above0 > CO_TRACER_FLOOR_PPM and 0.02 < above1 < above0:
                fracs.append(iv.open_frac.get(opening_id, 0.0))
                rates.append(-math.log(above1 / above0) / iv.dt_h)
                groups.append(iv.episode_id or f"day:{int(iv.t0 // 86400)}")
        prev = iv
    return np.array(fracs), np.array(rates), groups


def fit_tracer(
    frac: np.ndarray, rate: np.ndarray, *, tau: float = TAU_TRACER, rounds: int = 8
) -> TracerFit:
    """Regress the decay constant on the open fraction: ``K = k_co + f*theta``.

    Weighted towards the fast side of the scatter. An indoor source running
    during a decay makes it look slower; nothing makes it look faster, so the
    fast side is the side telling the truth.
    """
    fit = TracerFit(n_total=int(frac.size), n_open=int(np.sum(frac > 0.4)))
    if frac.size < 20:
        return fit

    X = np.column_stack([np.ones_like(frac), frac])
    try:
        beta = np.linalg.lstsq(X, rate, rcond=None)[0]
    except np.linalg.LinAlgError:
        return fit
    for _ in range(rounds):
        residual = rate - X @ beta
        scale = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        scale = max(scale, 1e-6)
        w = np.where(residual > 0, tau, 1.0 - tau) * np.minimum(
            1.0, 2.5 / np.maximum(np.abs(residual) / scale, 1e-9)
        )
        root = np.sqrt(w)
        try:
            beta = np.linalg.lstsq(X * root[:, None], rate * root, rcond=None)[0]
        except np.linalg.LinAlgError:
            break
    fit.sealed_loss = float(beta[0])
    fit.exchange = float(max(beta[1], 0.0))
    return fit


# --- stage 3: what the air arriving through the opening carries --------------


@dataclass
class OpeningEstimate:
    opening_id: str
    label: str
    orientation: str | None
    exchange: float = 0.0  # theta_w, air changes per hour
    exchange_ci: tuple[float, float] = (0.0, 0.0)
    coupling: float = 0.0  # g_w, ug/m3 per event/min
    coupling_ci: tuple[float, float] = (0.0, 0.0)
    background: float = 0.0  # ug/m3 outside this opening on a quiet street
    n_intervals: int = 0
    n_episodes: int = 0
    n_tracer: int = 0
    activity_spread: float = 0.0
    c0_spread: float = 0.0
    reported: bool = False
    reason: str = "no data yet"

    @property
    def refresh_minutes(self) -> float:
        """Minutes to replace half the room's air through this opening."""
        return 60.0 * math.log(2.0) / self.exchange if self.exchange > 1e-3 else 0.0

    def driving(self, activity: float) -> float:
        """What the air coming through this opening carries, ug/m3."""
        return self.background + self.coupling * activity

    def as_dict(self) -> dict:
        return {
            "opening_id": self.opening_id,
            "label": self.label,
            "orientation": self.orientation,
            "exchange": round(self.exchange, 3),
            "exchange_ci": [round(v, 3) for v in self.exchange_ci],
            "coupling": round(self.coupling, 3),
            "coupling_ci": [round(v, 3) for v in self.coupling_ci],
            "background": round(self.background, 2),
            "refresh_minutes": round(self.refresh_minutes, 1),
            "n_intervals": self.n_intervals,
            "n_episodes": self.n_episodes,
            "n_tracer": self.n_tracer,
            "activity_spread": round(self.activity_spread, 3),
            "c0_spread": round(self.c0_spread, 2),
            "reported": self.reported,
            "reason": self.reason,
        }


@dataclass
class HomeModel:
    #: What the air outside this home carries with the street quiet, ug/m3. One
    #: number for the whole home: see :func:`fit_incoming`.
    background: float = 0.0
    #: The sealed-home floor that ``background`` was measured against. A later
    #: floor twice this one means the air outside is twice as dirty as it was
    #: during the fit, and the forecast scales with it.
    background_ref: float = 0.0
    baseline: BaselineFit = field(default_factory=BaselineFit)
    openings: dict[str, OpeningEstimate] = field(default_factory=dict)
    tracers: dict[str, TracerFit] = field(default_factory=dict)
    #: Paired bootstrap draws per opening, ``(exchange, coupling)``. Kept on the
    #: model so the planner can push a low-end exchange rate through the
    #: forecast with the coupling that was fitted under it, without paying for a
    #: second bootstrap.
    draws: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    rho: float = DEFAULT_RHO
    n_open_intervals: int = 0
    n_indoor_source: int = 0
    fitted: bool = False

    @property
    def loss_rate(self) -> float:
        return self.baseline.loss_rate

    @property
    def outdoor_estimate(self) -> float:
        """Outdoor PM2.5 with the street quiet. A by-product of the fit, not an
        input: SASH has no outdoor sensor and never asks for one."""
        return self.background

    @property
    def co_sealed_loss(self) -> float:
        vals = [t.sealed_loss for t in self.tracers.values() if t.sealed_loss > 0]
        return float(np.median(vals)) if vals else 0.0

    def reported_openings(self) -> list[OpeningEstimate]:
        return [o for o in self.openings.values() if o.reported]

    def as_dict(self) -> dict:
        return {
            "loss_rate": round(self.baseline.loss_rate, 3),
            "sealed_floor": round(self.baseline.recent_floor, 2),
            "sealed_half_life_min": round(self.baseline.half_life_min, 1),
            "co_sealed_loss": round(self.co_sealed_loss, 3),
            "outdoor_estimate": round(self.outdoor_estimate, 2),
            "n_quiet_intervals": self.baseline.n,
            "n_open_intervals": self.n_open_intervals,
            "n_indoor_source": self.n_indoor_source,
            "fitted": self.fitted,
            "openings": {k: v.as_dict() for k, v in self.openings.items()},
            "tracers": {k: v.as_dict() for k, v in self.tracers.items()},
        }


def isolated_intervals(
    intervals: Sequence[Interval], opening_id: str, all_ids: Sequence[str]
) -> list[Interval]:
    """Intervals where this opening, and only this opening, was open.

    Two windows open at once are one ventilation event, not two, and a joint fit
    would hand one window's behaviour to the other.
    """
    others = [o for o in all_ids if o != opening_id]
    return [
        iv
        for iv in intervals
        if iv.usable
        and iv.camera_online
        and not iv.indoor_source
        and iv.open_frac.get(opening_id, 0.0) > 0.5
        and all(iv.open_frac.get(o, 0.0) < 0.15 for o in others)
    ]


def fit_incoming(
    rows_by_opening: dict[str, Sequence[Interval]],
    baseline: BaselineFit,
    exchange: dict[str, float],
) -> tuple[float, dict[str, float], float]:
    """``(background, {opening: coupling}, reference floor)``, at exchange rates
    the tracer supplied.

    With each exchange rate already measured, the term it multiplies moves to the
    left-hand side and what remains is linear in the quantities that describe the
    air outside: what it carries when the street is quiet, and how much each
    vehicle-motion event per minute adds to that.

    **The quiet street is one street.** All the openings of a flat give onto the
    same regional air on a still night, and what separates them is how much of
    the road they are exposed to. So the background is one shared column fitted
    across every opening at once, and only the street coupling is per opening.
    Fitting a private background per opening leaves the two terms trading off
    inside each opening separately, and then the ranking a household is shown
    depends on which window happened to be used on the dirty weeks.

    The shared term rides the home's own measured floor rather than a constant,
    so a week of dirty weather raises every opening together instead of being
    charged to whichever window was open during it.
    """
    ids = [
        oid
        for oid, rows in rows_by_opening.items()
        if rows and exchange.get(oid, 0.0) > 1e-3
    ]
    if not ids:
        return 0.0, {}, 1.0

    b = baseline.loss_rate
    all_star = [
        baseline.c_star(iv.t0) for oid in ids for iv in rows_by_opening[oid]
    ]
    star_med = float(np.median(all_star)) or 1.0

    ys: list[np.ndarray] = []
    shared: list[np.ndarray] = []
    per_opening: dict[str, list[np.ndarray]] = {oid: [] for oid in ids}
    for oid in ids:
        rows = rows_by_opening[oid]
        theta = exchange[oid]
        frac = np.array([iv.open_frac.get(oid, 0.0) for iv in rows])
        c0 = np.array([iv.c0 for iv in rows])
        c1 = np.array([iv.c1 for iv in rows])
        act = np.array([iv.activity for iv in rows])
        dt_h = np.array([iv.dt_h for iv in rows])
        star = np.array([baseline.c_star(iv.t0) for iv in rows])

        decay, weight = response(b + frac * theta, dt_h)
        admitted = weight * frac * theta
        ys.append(c1 - (c0 * decay + weight * b * star))
        shared.append(admitted * (star / star_med))
        for other in ids:
            per_opening[other].append(
                admitted * act if other == oid else np.zeros_like(act)
            )

    y = np.concatenate(ys)
    design = np.column_stack(
        [np.concatenate(shared)] + [np.concatenate(per_opening[oid]) for oid in ids]
    )
    beta, _, _ = onesided_nnls(design, y)
    return (
        float(beta[0]),
        {oid: float(beta[i + 1]) for i, oid in enumerate(ids)},
        star_med,
    )


def fit(
    intervals: Sequence[Interval],
    openings: Sequence[tuple[str, str, str | None]],
    *,
    rho: float = DEFAULT_RHO,
    bootstrap: int = 60,
    seed: int = 11,
) -> HomeModel:
    """Fit the household model. ``openings`` is ``(id, label, orientation)``."""
    model = HomeModel(rho=rho)
    ids = [o[0] for o in openings]
    model.openings = {
        oid: OpeningEstimate(opening_id=oid, label=label, orientation=orientation)
        for oid, label, orientation in openings
    }

    baseline = fit_baseline(intervals)
    model.baseline = baseline
    model.n_indoor_source = sum(1 for iv in intervals if iv.indoor_source)
    if baseline.n < 8 or baseline.loss_rate <= 1e-6:
        for est in model.openings.values():
            est.reason = (
                f"the sealed-home baseline needs {max(0, 8 - baseline.n)} more quiet readings"
            )
        return model

    model.fitted = True
    rng = np.random.default_rng(seed)

    rows_by: dict[str, list[Interval]] = {}
    for oid in ids:
        est = model.openings[oid]

        frac, rate, _ = tracer_decays(intervals, oid, ids)
        tracer = fit_tracer(frac, rate)
        model.tracers[oid] = tracer
        est.exchange = tracer.exchange
        est.n_tracer = tracer.n_open

        rows = isolated_intervals(intervals, oid, ids)
        rows_by[oid] = rows
        est.n_intervals = len(rows)
        est.n_episodes = len({iv.episode_id for iv in rows if iv.episode_id})
        model.n_open_intervals += len(rows)
        if not rows:
            est.reason = "learning: this opening has not been used yet"
            continue

        acts = np.array([iv.activity for iv in rows])
        c0s = np.array([iv.c0 for iv in rows])
        est.activity_spread = float(np.percentile(acts, 90) - np.percentile(acts, 10))
        est.c0_spread = float(np.percentile(c0s, 90) - np.percentile(c0s, 10))

    exchange = {oid: model.openings[oid].exchange for oid in ids}
    model.background, couplings, model.background_ref = fit_incoming(
        rows_by, baseline, exchange
    )
    for oid in ids:
        est = model.openings[oid]
        est.coupling = couplings.get(oid, 0.0)
        est.background = model.background

    draws = bootstrap_draws(intervals, rows_by, ids, baseline, n=bootstrap, rng=rng)
    for oid in ids:
        est = model.openings[oid]
        pair = draws.get(oid)
        if pair is not None and pair[0].size:
            est.exchange_ci = confidence_interval(pair[0])
            est.coupling_ci = confidence_interval(pair[1])
            model.draws[oid] = pair
        if rows_by.get(oid):
            est.reported, est.reason = _gate(est, model.tracers[oid])

    return model


def _gate(est: OpeningEstimate, tracer: TracerFit) -> tuple[bool, str]:
    """What has to be true before SASH will put a number on a window.

    Each of these is a sentence the UI can show as it stands. A product that
    refuses to answer has to say what it is waiting for.
    """
    if tracer.n_open < MIN_TRACER_ROWS:
        need = MIN_TRACER_ROWS - tracer.n_open
        return False, (
            f"learning: {need} more airings that begin while the air still carries "
            "something SASH can follow"
        )
    if est.n_intervals < MIN_INTERVALS_PER_OPENING:
        need = MIN_INTERVALS_PER_OPENING - est.n_intervals
        return False, f"learning: {need} more readings while only this opening is open"
    if est.n_episodes < MIN_EPISODES:
        need = MIN_EPISODES - est.n_episodes
        return False, f"learning: {need} more separate airings through this opening"
    if est.c0_spread < MIN_C0_SPREAD:
        return (
            False,
            "only ever used at one indoor level, so its air exchange cannot be "
            "separated from the air outside it",
        )
    if est.activity_spread < MIN_ACTIVITY_SPREAD:
        return (
            False,
            "only ever used when the street was equally busy, so its street "
            "coupling is not identified",
        )
    if est.exchange <= 1e-3:
        return False, "no measurable air exchange through this opening yet"
    return True, "estimated from this home's own history"


def confidence_interval(
    samples: np.ndarray, level: float = 0.90
) -> tuple[float, float]:
    if samples.size == 0:
        return (0.0, 0.0)
    lo = float(np.percentile(samples, 100 * (1 - level) / 2))
    hi = float(np.percentile(samples, 100 * (1 + level) / 2))
    return (lo, hi)


def bootstrap_draws(
    intervals: Sequence[Interval],
    rows_by_opening: dict[str, Sequence[Interval]],
    all_ids: Sequence[str],
    baseline: BaselineFit,
    *,
    n: int,
    rng: np.random.Generator,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Block bootstrap over airings, resampling both stages in one draw.

    Resampling readings independently would treat the three readings inside one
    forty-five-minute airing as three independent airings and halve every
    interval, so the block is the airing.

    The two stages are drawn together and the second stage is refitted across
    every opening at once, exactly as the point estimate was. That matters for
    what comes out: an exchange rate at the low end of its interval belongs with
    the coupling that was fitted under it, and a coupling belongs with the shared
    background that the other openings' draws put under it. The pairs can then be
    pushed straight through the forecast.
    """
    ids = [oid for oid in all_ids if rows_by_opening.get(oid)]
    empty: dict[str, tuple[np.ndarray, np.ndarray]] = {
        oid: (np.array([]), np.array([])) for oid in all_ids
    }
    if n <= 0 or not ids:
        return empty

    tracer_data: dict[str, tuple[np.ndarray, np.ndarray, dict[str, list[int]]]] = {}
    pm_blocks: dict[str, dict[str, list[Interval]]] = {}
    for oid in ids:
        frac, rate, groups = tracer_decays(intervals, oid, all_ids)
        blocks: dict[str, list[int]] = {}
        for i, key in enumerate(groups):
            blocks.setdefault(key, []).append(i)
        tracer_data[oid] = (frac, rate, blocks)

        episodes: dict[str, list[Interval]] = {}
        for iv in rows_by_opening[oid]:
            episodes.setdefault(iv.episode_id or f"solo:{iv.t0}", []).append(iv)
        pm_blocks[oid] = episodes

    usable = [
        oid
        for oid in ids
        if len(tracer_data[oid][2]) >= 4 and len(pm_blocks[oid]) >= 4
    ]
    if not usable:
        return empty

    thetas = {oid: np.zeros(n) for oid in usable}
    couplings = {oid: np.zeros(n) for oid in usable}
    for i in range(n):
        drawn_theta: dict[str, float] = {}
        drawn_rows: dict[str, list[Interval]] = {}
        for oid in usable:
            frac, rate, blocks = tracer_data[oid]
            keys = list(blocks)
            picked: list[int] = []
            for key in rng.choice(keys, size=len(keys), replace=True):
                picked.extend(blocks[key])
            drawn_theta[oid] = fit_tracer(frac[picked], rate[picked]).exchange

            episodes = pm_blocks[oid]
            ekeys = list(episodes)
            rows: list[Interval] = []
            for key in rng.choice(ekeys, size=len(ekeys), replace=True):
                rows.extend(episodes[key])
            drawn_rows[oid] = rows
        try:
            _bg, gs, _ref = fit_incoming(drawn_rows, baseline, drawn_theta)
        except (np.linalg.LinAlgError, ValueError):
            gs = {}
        for oid in usable:
            thetas[oid][i] = drawn_theta[oid]
            couplings[oid][i] = gs.get(oid, 0.0)

    out = dict(empty)
    for oid in usable:
        out[oid] = (thetas[oid], couplings[oid])
    return out


# --- using the model --------------------------------------------------------


def driving_concentration(
    model: HomeModel, opening_id: str, activity: float, c_star: float
) -> float:
    """Concentration of the air arriving through an opening, ug/m3.

    The quiet-street background is scaled by how the sealed-home floor has moved
    since the fit, so a dirty week raises every opening together.
    """
    est = model.openings.get(opening_id)
    if est is None:
        return c_star
    reference = model.background_ref or model.baseline.recent_floor
    shape = (c_star / reference) if reference else 1.0
    return est.background * shape + est.coupling * activity


def predict_rate(
    model: HomeModel,
    c0: float,
    open_frac: dict[str, float],
    activity: float,
    c_star: float,
) -> float:
    """Model-implied dC/dt, ug/m3 per hour."""
    rate = model.baseline.loss_rate * (c_star - c0)
    for oid, frac in open_frac.items():
        est = model.openings.get(oid)
        if est is None or frac <= 0.0 or est.exchange <= 0.0:
            continue
        driving = driving_concentration(model, oid, activity, c_star)
        rate += frac * est.exchange * (driving - c0)
    return rate


def integrate(model: HomeModel, c_start: float, steps) -> list[float]:
    """Forward-integrate the fitted model.

    ``steps`` yields ``(dt_hours, open_fraction_by_opening, activity, c_star)``.
    Each step is subdivided: an open window drives the removal rate high enough
    that a quarter-hour Euler step is not stable.
    """
    c = c_start
    out = [c]
    for dt_h, frac, activity, star in steps:
        sub = max(1, int(math.ceil(dt_h / 0.02)))
        h = dt_h / sub
        for _ in range(sub):
            c = max(0.0, c + h * predict_rate(model, c, frac, activity, star))
        out.append(c)
    return out
