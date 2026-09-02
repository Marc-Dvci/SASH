"""A physical home, used as ground truth.

SASH estimates two numbers per opening from a household's own history: how fast
that opening exchanges air, and how strongly street activity contaminates the
air it lets in. To know whether the estimator works, something has to know the
right answer. This module is that something.

It is an ordinary single-zone mass balance, the standard indoor-air model:

    dC/dt = sum_w o_w(t) * lam_w * (B(t) + g_w * A(t) - C)      openings
          + lam_base * (P * B(t) - C)                            envelope leakage
          - k * C                                                deposition
          + s(t)                                                 indoor sources

with

    C     indoor PM2.5, ug/m3
    B(t)  regional background PM2.5, ug/m3
    A(t)  street activity, in vehicle-motion events per minute
    o_w   1 while opening w is open
    lam_w air exchange rate through opening w, per hour
    g_w   street coupling of opening w, ug/m3 per event/min
    P     envelope penetration factor
    k     deposition rate, per hour
    s(t)  indoor emission, ug/m3 per hour

Temperature, relative humidity and CO ride the same equation with their own
sources and their own outdoor values, because the Air Quality Monitor reports
all four and a ventilation decision that ignores humidity is not a ventilation
decision.

Nothing in ``src/sash`` imports this module. It generates the Ring events and
sensor readings that the product consumes, and the benchmark reads its
``ground_truth`` to score the estimator. The product never sees it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np

MINUTES_PER_HOUR = 60.0


@dataclass
class OpeningParams:
    """One window or door, with a contact sensor on it."""

    opening_id: str
    label: str
    orientation: str  # street | courtyard | garden
    lam: float  # air exchange rate when open, per hour
    g: float  # street coupling, ug/m3 per (event/min)
    habit_weight: float = 1.0  # how often the household reaches for this one


@dataclass
class HomeParams:
    lam_base: float = 0.35  # envelope leakage, per hour
    penetration: float = 0.7  # fraction of outdoor PM crossing a closed envelope
    deposition: float = 0.30  # PM2.5 loss to surfaces, per hour
    volume_m3: float = 160.0
    openings: list[OpeningParams] = field(default_factory=list)
    # occupant behaviour
    morning_air_hour: float = 8.1
    evening_air_hour: float = 19.2
    airing_prob: float = 0.85
    cook_hours: tuple[float, ...] = (12.4, 19.6)
    # comfort
    rh_outdoor_mean: float = 68.0
    temp_setpoint_c: float = 21.0


@dataclass
class SimResult:
    t0: float  # epoch seconds of minute 0
    minutes: int
    pm_in: np.ndarray
    rh_in: np.ndarray
    temp_in: np.ndarray
    co_in: np.ndarray
    background: np.ndarray
    activity: np.ndarray  # ground-truth events/min intensity
    open_mask: dict[str, np.ndarray]
    vehicle_event_times: list[float]
    open_intervals: dict[str, list[tuple[float, float]]]
    aqm_reports: list[dict[str, Any]]
    camera_offline: list[tuple[float, float]]
    aqm_offline: list[tuple[float, float]]
    indoor_source: np.ndarray
    ground_truth: dict[str, Any]
    # The exogenous series, kept so the home can be re-integrated forward from
    # any minute with a different opening schedule. That is what makes "the
    # household follows the plan" a physical question rather than a redrawing.
    source_moisture: np.ndarray = field(default_factory=lambda: np.array([]))
    source_co: np.ndarray = field(default_factory=lambda: np.array([]))
    temp_out: np.ndarray = field(default_factory=lambda: np.array([]))
    vp_out: np.ndarray = field(default_factory=lambda: np.array([]))
    aqm_interval_min: int = 15
    aqm_average_min: int = 3

    def time_at(self, minute: int) -> float:
        return self.t0 + minute * 60.0


def default_home(rng: np.random.Generator | None = None) -> HomeParams:
    """The demo household: a street-facing flat with a courtyard side.

    The numbers are the point of the product. The street window moves more air
    (bigger opening, on the windward face) *and* costs far more particle intake.
    Neither window is simply better, which is exactly why a household cannot
    reason about this without measurement.
    """
    return HomeParams(
        lam_base=0.32,
        penetration=0.70,
        deposition=0.30,
        openings=[
            OpeningParams("win_street", "Street window", "street", lam=4.2, g=8.0, habit_weight=3.0),
            OpeningParams("win_court", "Courtyard window", "courtyard", lam=3.1, g=0.8, habit_weight=1.0),
            OpeningParams("door_balcony", "Balcony door", "street", lam=6.0, g=5.6, habit_weight=0.7),
        ],
    )


def random_home(rng: np.random.Generator) -> HomeParams:
    """A random plausible household, for the benchmark."""
    n_open = int(rng.integers(2, 4))
    orientations = ["street", "courtyard", "garden"]
    openings = []
    for i in range(n_open):
        orientation = orientations[i] if i < len(orientations) else "other"
        street_side = orientation == "street"
        openings.append(
            OpeningParams(
                opening_id=f"win_{i}",
                label=f"Opening {i + 1}",
                orientation=orientation,
                lam=float(rng.uniform(1.6, 6.5)),
                g=float(rng.uniform(3.0, 12.0) if street_side else rng.uniform(0.0, 2.4)),
                habit_weight=float(rng.uniform(0.5, 3.0)),
            )
        )
    return HomeParams(
        lam_base=float(rng.uniform(0.15, 0.55)),
        penetration=float(rng.uniform(0.55, 0.85)),
        deposition=float(rng.uniform(0.18, 0.45)),
        openings=openings,
        morning_air_hour=float(rng.uniform(7.0, 9.5)),
        evening_air_hour=float(rng.uniform(17.5, 21.0)),
        airing_prob=float(rng.uniform(0.6, 0.95)),
    )


def null_home(rng: np.random.Generator) -> HomeParams:
    """Two openings that are physically identical.

    The negative control. SASH must not announce that one of these is the better
    window, however tempting the noise looks.
    """
    lam = float(rng.uniform(2.0, 5.0))
    g = float(rng.uniform(2.5, 9.0))
    return HomeParams(
        lam_base=float(rng.uniform(0.15, 0.55)),
        deposition=float(rng.uniform(0.18, 0.45)),
        openings=[
            OpeningParams("win_a", "Opening A", "street", lam=lam, g=g, habit_weight=1.4),
            OpeningParams("win_b", "Opening B", "street", lam=lam, g=g, habit_weight=1.0),
        ],
    )


# --- exogenous drivers ------------------------------------------------------


def street_activity(minutes: int, t0: float, rng: np.random.Generator) -> np.ndarray:
    """Vehicle-motion event intensity, events per minute.

    Two commute peaks on weekdays, a flatter weekend, a night floor, and a slow
    day-to-day multiplier so no two days are the same.
    """
    idx = np.arange(minutes)
    hour = ((t0 / 3600.0) + idx / 60.0) % 24.0
    day_index = np.floor((t0 / 86400.0) + idx / 1440.0).astype(int)
    weekday = (day_index % 7) < 5

    def bump(centre: float, width: float, height: float) -> np.ndarray:
        delta = np.minimum(np.abs(hour - centre), 24.0 - np.abs(hour - centre))
        return height * np.exp(-0.5 * (delta / width) ** 2)

    weekday_shape = (
        0.09
        + bump(8.2, 1.05, 1.05)
        + bump(12.6, 1.5, 0.32)
        + bump(17.9, 1.35, 0.90)
        + bump(21.5, 1.8, 0.16)
    )
    weekend_shape = 0.09 + bump(11.5, 2.6, 0.42) + bump(18.5, 2.4, 0.36)
    shape = np.where(weekday, weekday_shape, weekend_shape)

    unique_days = np.unique(day_index)
    day_multiplier = {d: float(rng.uniform(0.8, 1.25)) for d in unique_days}
    multiplier = np.array([day_multiplier[d] for d in day_index])

    # Short-lived local fluctuation: a delivery van idling, a queue at a light.
    wobble = np.exp(0.22 * _ou_process(minutes, theta=0.02, sigma=0.35, rng=rng))
    return np.clip(shape * multiplier * wobble, 0.01, None)


def background_pm(minutes: int, t0: float, rng: np.random.Generator) -> np.ndarray:
    """Regional background PM2.5: slow weather, plus a diurnal boundary layer."""
    idx = np.arange(minutes)
    hour = ((t0 / 3600.0) + idx / 60.0) % 24.0
    weather = 9.0 * np.exp(_ou_process(minutes, theta=0.0012, sigma=0.06, rng=rng))
    diurnal = 1.0 + 0.22 * np.cos((hour - 6.0) * math.pi / 12.0)
    return np.clip(weather * diurnal, 2.0, 120.0)


def _ou_process(n: int, theta: float, sigma: float, rng: np.random.Generator) -> np.ndarray:
    """Discrete Ornstein-Uhlenbeck path, mean zero."""
    out = np.zeros(n)
    noise = rng.normal(0.0, sigma, size=n)
    for i in range(1, n):
        out[i] = out[i - 1] * (1.0 - theta) + noise[i]
    return out


# --- occupant behaviour -----------------------------------------------------


def _plan_openings(
    params: HomeParams, minutes: int, t0: float, rng: np.random.Generator
) -> dict[str, np.ndarray]:
    """Decide when each opening is open.

    The household has a habit, not a policy: it airs the flat in the morning and
    in the evening, usually through the same window, and opens something while
    cooking. The habit is strong enough to be a real habit and loose enough that
    every opening is eventually observed at several activity levels, which is
    what makes the per-opening parameters identifiable at all.
    """
    masks = {o.opening_id: np.zeros(minutes, dtype=bool) for o in params.openings}
    weights = np.array([o.habit_weight for o in params.openings], dtype=float)
    weights = weights / weights.sum()
    ids = [o.opening_id for o in params.openings]

    n_days = int(math.ceil(minutes / 1440.0)) + 1
    start_hour = (t0 / 3600.0) % 24.0
    for day in range(n_days):
        day_start_min = int((day * 24.0 - start_hour) * 60.0)
        for centre_hour, spread, dur_lo, dur_hi in (
            (params.morning_air_hour, 0.55, 15, 45),
            (params.evening_air_hour, 0.8, 20, 60),
        ):
            if rng.random() > params.airing_prob:
                continue
            start = day_start_min + int((centre_hour + rng.normal(0.0, spread)) * 60.0)
            duration = int(rng.integers(dur_lo, dur_hi))
            # Habit picks the window most of the time; sometimes the other one.
            if rng.random() < 0.72:
                choice = ids[int(rng.choice(len(ids), p=weights))]
            else:
                choice = ids[int(rng.integers(0, len(ids)))]
            _mark(masks[choice], start, duration, minutes)
            # Occasionally both sides at once, for cross ventilation.
            if len(ids) > 1 and rng.random() < 0.18:
                other = ids[int(rng.integers(0, len(ids)))]
                if other != choice:
                    _mark(masks[other], start + int(rng.integers(0, 10)), duration // 2, minutes)

        for cook_hour in params.cook_hours:
            if rng.random() < 0.55:
                start = day_start_min + int((cook_hour + rng.normal(0.0, 0.4)) * 60.0)
                choice = ids[int(rng.integers(0, len(ids)))]
                _mark(masks[choice], start, int(rng.integers(10, 35)), minutes)

        if rng.random() < 0.35:
            start = day_start_min + int(rng.uniform(9.0, 22.0) * 60.0)
            choice = ids[int(rng.integers(0, len(ids)))]
            _mark(masks[choice], start, int(rng.integers(10, 90)), minutes)

    return masks


def _mark(mask: np.ndarray, start: int, duration: int, minutes: int) -> None:
    lo = max(0, start)
    hi = min(minutes, start + max(1, duration))
    if hi > lo:
        mask[lo:hi] = True


def _indoor_sources(
    params: HomeParams, minutes: int, t0: float, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cooking, showers and the occasional candle.

    Returns PM2.5, moisture and CO emission rates, all per hour.
    """
    pm = np.zeros(minutes)
    moisture = np.zeros(minutes)
    co = np.zeros(minutes)
    start_hour = (t0 / 3600.0) % 24.0
    n_days = int(math.ceil(minutes / 1440.0)) + 1

    for day in range(n_days):
        day_start_min = int((day * 24.0 - start_hour) * 60.0)
        for cook_hour in params.cook_hours:
            if rng.random() < 0.85:
                start = day_start_min + int((cook_hour + rng.normal(0.0, 0.35)) * 60.0)
                duration = int(rng.integers(18, 55))
                strength = float(rng.uniform(70.0, 380.0))
                _add(pm, start, duration, strength, minutes)
                _add(moisture, start, duration, float(rng.uniform(4.0, 11.0)), minutes)
                _add(co, start, duration, float(rng.uniform(0.4, 2.2)), minutes)
        # showers
        for _ in range(int(rng.integers(1, 3))):
            start = day_start_min + int(rng.uniform(6.5, 22.0) * 60.0)
            _add(moisture, start, int(rng.integers(10, 25)), float(rng.uniform(8.0, 18.0)), minutes)
        # candles / toaster / vacuum
        if rng.random() < 0.30:
            start = day_start_min + int(rng.uniform(17.0, 23.0) * 60.0)
            _add(pm, start, int(rng.integers(25, 120)), float(rng.uniform(25.0, 110.0)), minutes)

    return pm, moisture, co


def _add(arr: np.ndarray, start: int, duration: int, value: float, minutes: int) -> None:
    lo = max(0, start)
    hi = min(minutes, start + max(1, duration))
    if hi > lo:
        arr[lo:hi] += value


# --- the simulation ---------------------------------------------------------


def simulate(
    params: HomeParams,
    *,
    days: float = 21.0,
    t0: float,
    seed: int = 7,
    aqm_interval_min: int = 15,
    aqm_average_min: int = 3,
    aqm_miss_prob: float = 0.04,
    pm_noise_ug: float = 0.45,
    camera_outage_per_week: float = 0.7,
) -> SimResult:
    """Integrate the home forward on a one-minute grid and emit sensor data."""
    rng = np.random.default_rng(seed)
    minutes = int(days * 1440)

    activity = street_activity(minutes, t0, rng)
    background = background_pm(minutes, t0, rng)
    open_mask = _plan_openings(params, minutes, t0, rng)
    src_pm, src_moisture, src_co = _indoor_sources(params, minutes, t0, rng)

    # Outdoor comfort channels.
    hour = ((t0 / 3600.0) + np.arange(minutes) / 60.0) % 24.0
    day_phase = np.cos((hour - 15.0) * math.pi / 12.0)
    temp_out = 11.0 + 5.5 * day_phase + 2.0 * _ou_process(minutes, 0.0015, 0.05, rng)
    rh_out = np.clip(params.rh_outdoor_mean - 12.0 * day_phase, 30.0, 98.0)

    # Outdoor air warmed to room temperature carries the same water but a lower
    # relative humidity. That is why airing a flat in winter dries it out, and
    # it is half the reason the household opens a window at all.
    vp_out = rh_out * _sat_vp_hpa(temp_out) / 100.0

    pm_in = np.zeros(minutes)
    rh_in = np.zeros(minutes)
    temp_in = np.zeros(minutes)
    co_in = np.zeros(minutes)
    pm_in[0] = background[0] * 0.6
    rh_in[0] = 48.0
    temp_in[0] = params.temp_setpoint_c
    co_in[0] = 0.15

    lam_by_id = {o.opening_id: o.lam for o in params.openings}
    g_by_id = {o.opening_id: o.g for o in params.openings}

    integrate_home(
        params,
        open_mask,
        background,
        activity,
        src_pm,
        src_moisture,
        src_co,
        temp_out,
        vp_out,
        pm_in,
        rh_in,
        temp_in,
        co_in,
        start=1,
    )

    # --- device outages ---------------------------------------------------
    camera_offline = _outages(minutes, t0, camera_outage_per_week * days / 7.0, rng, 25, 180)
    aqm_offline = _outages(minutes, t0, 0.4 * days / 7.0, rng, 40, 240)

    # --- camera: vehicle-motion events -----------------------------------
    # One passing vehicle does not produce one event. The camera fires on
    # movement inside the configured zone, so a slow vehicle can trigger twice
    # and a distant one not at all. The simulator reproduces that on purpose:
    # the product must never present the count as a vehicle count.
    vehicle_event_times: list[float] = []
    for i in range(minutes):
        t = t0 + i * 60.0
        if _inside(t, camera_offline):
            continue
        expected = activity[i] * 0.86  # detection shortfall
        n = rng.poisson(expected)
        for _ in range(int(n)):
            offset = float(rng.uniform(0, 60))
            vehicle_event_times.append(t + offset)
            if rng.random() < 0.17:  # re-trigger on the same pass
                vehicle_event_times.append(t + offset + float(rng.uniform(1.5, 7.0)))
    vehicle_event_times.sort()

    # --- contact sensors: transitions -------------------------------------
    open_intervals: dict[str, list[tuple[float, float]]] = {}
    for oid, mask in open_mask.items():
        intervals = []
        i = 0
        while i < minutes:
            if mask[i]:
                start = i
                while i < minutes and mask[i]:
                    i += 1
                intervals.append((t0 + start * 60.0, t0 + i * 60.0))
            else:
                i += 1
        open_intervals[oid] = intervals

    # --- Air Quality Monitor: a reading every 15 minutes -------------------
    aqm_reports = []
    for i in range(0, minutes, aqm_interval_min):
        t = t0 + i * 60.0
        if _inside(t, aqm_offline) or rng.random() < aqm_miss_prob:
            continue
        aqm_reports.append(
            {
                "reported_at": t,
                # The device transmits every ``aqm_interval_min``; what it
                # transmits is modelled as a short trailing average, whose
                # length Ring's documentation does not state. The benchmark
                # sweeps this because it changes what is identifiable.
                "pm2_5": round(
                    float(np.mean(pm_in[max(0, i - aqm_average_min + 1) : i + 1]))
                    + float(rng.normal(0.0, pm_noise_ug)),
                    1,
                ),
                # The device does not report a negative concentration, so the
                # noise is applied and then floored the way the firmware would.
                "co": round(
                    max(
                        0.0,
                        float(np.mean(co_in[max(0, i - aqm_average_min + 1) : i + 1]))
                        + float(rng.normal(0.0, 0.02)),
                    ),
                    2,
                ),
                "temperature": round(float(temp_in[i]) + float(rng.normal(0.0, 0.15)), 1),
                "humidity": round(float(np.mean(rh_in[max(0, i - aqm_average_min + 1): i + 1])) + float(rng.normal(0.0, 0.6)), 1),
            }
        )

    ground_truth = {
        "lam_base": params.lam_base,
        "penetration": params.penetration,
        "deposition": params.deposition,
        "openings": {
            o.opening_id: {
                "label": o.label,
                "orientation": o.orientation,
                "lam": o.lam,
                "g": o.g,
                "phi": o.lam * o.g,
            }
            for o in params.openings
        },
        "params": asdict(params),
    }

    return SimResult(
        t0=t0,
        minutes=minutes,
        pm_in=pm_in,
        rh_in=rh_in,
        temp_in=temp_in,
        co_in=co_in,
        background=background,
        activity=activity,
        open_mask=open_mask,
        vehicle_event_times=vehicle_event_times,
        open_intervals=open_intervals,
        aqm_reports=aqm_reports,
        camera_offline=camera_offline,
        aqm_offline=aqm_offline,
        indoor_source=src_pm,
        ground_truth=ground_truth,
        source_moisture=src_moisture,
        source_co=src_co,
        temp_out=temp_out,
        vp_out=vp_out,
        aqm_interval_min=aqm_interval_min,
        aqm_average_min=aqm_average_min,
    )


def true_intake(
    result: SimResult,
    params: HomeParams,
    opening_id: str,
    start_at: float,
    duration_min: float,
    *,
    recovery_min: float = 90.0,
) -> float:
    """What an airing would really cost, in ug/m3 x minutes above a shut home.

    The ground-truth answer to the question the planner forecasts. The same
    minute grid is integrated twice from the same starting concentration, once
    with the opening open for ``duration_min`` and once with the home shut
    throughout, and the area between them is returned. Everything else -- the
    regional background, the street, the indoor sources -- is the record that
    actually happened, so the only difference between the two runs is the
    opening.

    The benchmark scores SASH's choice with this, never with SASH's own model.
    """
    lam_by_id = {o.opening_id: o.lam for o in params.openings}
    g_by_id = {o.opening_id: o.g for o in params.openings}
    if opening_id not in lam_by_id:
        raise KeyError(opening_id)
    lam_w = lam_by_id[opening_id]
    g_w = g_by_id[opening_id]

    i0 = int(round((start_at - result.t0) / 60.0))
    if i0 < 0 or i0 >= result.minutes:
        return float("nan")
    span = int(round(duration_min + recovery_min))
    dt = 1.0 / MINUTES_PER_HOUR

    c_open = c_shut = float(result.pm_in[i0])
    intake = 0.0
    for k in range(span):
        i = i0 + k
        if i >= result.minutes:
            break
        b = float(result.background[i])
        a = float(result.activity[i])
        src = float(result.indoor_source[i])
        common_open = (
            params.lam_base * (params.penetration * b - c_open)
            - params.deposition * c_open
            + src
        )
        common_shut = (
            params.lam_base * (params.penetration * b - c_shut)
            - params.deposition * c_shut
            + src
        )
        driven = lam_w * (b + g_w * a - c_open) if k < duration_min else 0.0
        c_open = max(0.2, c_open + dt * (common_open + driven))
        c_shut = max(0.2, c_shut + dt * common_shut)
        intake += c_open - c_shut
    return intake


def _sat_vp_hpa(t_c):
    """Saturation vapour pressure, Magnus formula, hPa."""
    return 6.112 * np.exp(17.62 * np.asarray(t_c) / (243.12 + np.asarray(t_c)))


def integrate_home(
    params: HomeParams,
    open_mask: dict[str, np.ndarray],
    background: np.ndarray,
    activity: np.ndarray,
    src_pm: np.ndarray,
    src_moisture: np.ndarray,
    src_co: np.ndarray,
    temp_out: np.ndarray,
    vp_out: np.ndarray,
    pm_in: np.ndarray,
    rh_in: np.ndarray,
    temp_in: np.ndarray,
    co_in: np.ndarray,
    *,
    start: int = 1,
) -> None:
    """Integrate the four channels forward in place, from minute ``start``.

    One function, used both for the first pass and for re-running the home after
    an opening schedule changes, so the two can never drift apart.
    """
    dt = 1.0 / MINUTES_PER_HOUR
    minutes = len(pm_in)
    lam_by_id = {o.opening_id: o.lam for o in params.openings}
    g_by_id = {o.opening_id: o.g for o in params.openings}

    for i in range(max(start, 1), minutes):
        j = i - 1
        lam_open = 0.0
        driven_pm = 0.0
        for oid, mask in open_mask.items():
            if mask[j]:
                lam = lam_by_id[oid]
                lam_open += lam
                driven_pm += lam * (background[j] + g_by_id[oid] * activity[j])

        # PM2.5
        dpm = (
            driven_pm
            - lam_open * pm_in[j]
            + params.lam_base * (params.penetration * background[j] - pm_in[j])
            - params.deposition * pm_in[j]
            + src_pm[j]
        )
        pm_in[i] = max(0.2, pm_in[j] + dt * dpm)

        total_exchange = lam_open + params.lam_base

        # Relative humidity: incoming air is re-referenced to indoor temperature.
        rh_equiv = float(np.clip(100.0 * vp_out[j] / _sat_vp_hpa(temp_in[j]), 5.0, 100.0))
        drh = (
            total_exchange * (rh_equiv - rh_in[j])
            + src_moisture[j]
            - 0.05 * (rh_in[j] - 38.0)
        )
        rh_in[i] = float(np.clip(rh_in[j] + dt * drh, 18.0, 99.0))

        # Temperature: heating pulls to setpoint, ventilation pulls outdoors.
        dtemp = total_exchange * (temp_out[j] - temp_in[j]) + 3.0 * (
            params.temp_setpoint_c - temp_in[j]
        )
        temp_in[i] = temp_in[j] + dt * dtemp

        # CO: outdoor near zero, indoor combustion is the only source.
        dco = total_exchange * (0.05 - co_in[j]) + src_co[j] - 0.4 * co_in[j]
        co_in[i] = max(0.0, co_in[j] + dt * dco)


def aqm_report(
    result: "SimResult", minute: int, rng: np.random.Generator, pm_noise_ug: float = 0.45
) -> dict[str, Any]:
    """One Air Quality Monitor reading, built the way the device builds one."""
    lo = max(0, minute - result.aqm_average_min + 1)
    hi = minute + 1
    return {
        "reported_at": result.t0 + minute * 60.0,
        "pm2_5": round(
            float(np.mean(result.pm_in[lo:hi])) + float(rng.normal(0.0, pm_noise_ug)), 1
        ),
        "co": round(
            max(0.0, float(np.mean(result.co_in[lo:hi])) + float(rng.normal(0.0, 0.02))), 2
        ),
        "temperature": round(float(result.temp_in[minute]) + float(rng.normal(0.0, 0.15)), 1),
        "humidity": round(
            float(np.mean(result.rh_in[lo:hi])) + float(rng.normal(0.0, 0.6)), 1
        ),
    }


def follow_plan(
    result: SimResult,
    params: HomeParams,
    opening_id: str,
    start_at: float,
    duration_min: float,
    *,
    seed: int = 4242,
) -> int:
    """Have the household actually do what SASH suggested, and live with it.

    The opening is marked open for the plan's window and the home is
    re-integrated from that minute to the end of the tape, on the same regional
    background, the same street and the same indoor sources that were already
    drawn. Every reading after the plan starts is then rebuilt from the new
    trajectory. Nothing about the outside world changes; only the window does.

    Returns the number of readings that were rebuilt.
    """
    if opening_id not in result.open_mask:
        raise KeyError(opening_id)
    i0 = int(round((start_at - result.t0) / 60.0))
    i1 = min(result.minutes, i0 + int(round(duration_min)))
    if i0 < 0 or i1 <= i0:
        return 0

    result.open_mask[opening_id][i0:i1] = True

    spans: list[tuple[float, float]] = []
    mask = result.open_mask[opening_id]
    i = 0
    while i < result.minutes:
        if mask[i]:
            begin = i
            while i < result.minutes and mask[i]:
                i += 1
            spans.append((result.t0 + begin * 60.0, result.t0 + i * 60.0))
        else:
            i += 1
    result.open_intervals[opening_id] = spans

    integrate_home(
        params,
        result.open_mask,
        result.background,
        result.activity,
        result.indoor_source,
        result.source_moisture,
        result.source_co,
        result.temp_out,
        result.vp_out,
        result.pm_in,
        result.rh_in,
        result.temp_in,
        result.co_in,
        start=i0 + 1,
    )

    rng = np.random.default_rng(seed + i0)
    rebuilt = 0
    for row in result.aqm_reports:
        if row["reported_at"] <= start_at:
            continue
        minute = int(round((row["reported_at"] - result.t0) / 60.0))
        row.update(aqm_report(result, minute, rng))
        rebuilt += 1
    return rebuilt


def _outages(
    minutes: int, t0: float, expected: float, rng: np.random.Generator, lo: int, hi: int
) -> list[tuple[float, float]]:
    out = []
    for _ in range(int(rng.poisson(max(expected, 0.0)))):
        start = int(rng.integers(0, max(1, minutes - hi)))
        duration = int(rng.integers(lo, hi))
        out.append((t0 + start * 60.0, t0 + (start + duration) * 60.0))
    return sorted(out)


def _inside(t: float, intervals: list[tuple[float, float]]) -> bool:
    return any(lo <= t < hi for lo, hi in intervals)
