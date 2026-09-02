"""The household brain: everything the API and the UI read comes from here.

One object, rebuilt on demand and cached until new data arrives, holding the
observation table, the indoor-source flags, the fitted model, the street
activity index, the permutation comparisons and the current recommendation.
Keeping it in one place means the timeline, the recommendation and the ledger
can never disagree about what the model currently is.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .analytics import activity as activity_mod
from .analytics import (
    estimator,
    holdout,
    intervals as intervals_mod,
    nulls,
    planner,
    sources,
)
from .config import AQM_MAX_SAMPLE_AGE_MIN, CO_ALERT_PPM, PM25_ALERT_UG_M3
from .store import Store


@dataclass
class HouseholdView:
    built_at: float
    intervals: list[intervals_mod.Interval]
    model: estimator.HomeModel
    activity: activity_mod.ActivityIndex
    source_report: sources.SourceReport
    comparisons: list[nulls.Comparison] = field(default_factory=list)
    draws: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    calibration: holdout.Calibration = field(default_factory=holdout.Calibration)


class Household:
    """Owns the analysis for one linked Ring account."""

    def __init__(
        self,
        store: Store,
        *,
        rho: float = estimator.DEFAULT_RHO,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.rho = rho
        #: Where "now" is. Wall clock against a real Ring account; the position
        #: of the simulated tape when the demo is driving. Every reading of the
        #: current time in this class goes through it, so the demo shows the
        #: same household state on every run instead of drifting with whenever
        #: it happened to be started.
        self.clock = clock
        self._view: HouseholdView | None = None
        self._stamp: tuple[int, int, int] = (-1, -1, -1)
        self._backtest: holdout.Backtest | None = None
        self._backtest_stamp: tuple[int, int, int] = (-1, -1, -1)

    def now(self) -> float:
        return self.clock()

    # --- building ---------------------------------------------------------
    def invalidate(self) -> None:
        """Drop the cached view. Called when the device mapping changes, which
        changes what every downstream number means."""
        self._view = None
        self._stamp = (-1, -1, -1)

    def _fingerprint(self) -> tuple[int, int, int]:
        return (
            self.store.vehicle_event_count(),
            len(self.store.contact_events()),
            len(self.store.aq_samples()),
        )

    def view(self, *, force: bool = False, permutations: int = 300) -> HouseholdView:
        stamp = self._fingerprint()
        if self._view is not None and not force and stamp == self._stamp:
            return self._view

        rows = intervals_mod.build(self.store)
        report = sources.detect(rows)
        openings = [
            (d.device_id, d.label or d.ring_name, d.orientation)
            for d in self.store.devices(role="opening")
        ]
        model = estimator.fit(rows, openings, rho=self.rho)

        cameras = [d.device_id for d in self.store.devices(role="street_camera")]
        outages = intervals_mod._offline_windows(self.store, cameras)
        index = activity_mod.build(
            self.store.vehicle_event_times(), now=self.now(), offline_windows=outages
        )

        draws = {
            oid: pair for oid, pair in model.draws.items() if model.openings[oid].reported
        }

        comparisons = nulls.rank_openings(rows, model, permutations=permutations)
        calibration = holdout.calibrate(rows, openings)

        self._view = HouseholdView(
            built_at=self.now(),
            intervals=rows,
            model=model,
            activity=index,
            source_report=report,
            comparisons=comparisons,
            draws=draws,
            calibration=calibration,
        )
        self._stamp = stamp
        return self._view

    # --- what the UI asks for --------------------------------------------
    def status(self) -> dict[str, Any]:
        """Right now: the readings, the openings, the street."""
        now = self.now()
        sample = self.store.latest_aq_sample()
        age_min = (now - sample["reported_at"]) / 60.0 if sample else None
        openings = []
        for device in self.store.devices(role="opening"):
            events = [e for e in self.store.contact_events(device.device_id)]
            state = events[-1][2] if events else "closed"
            since = events[-1][1] if events else None
            openings.append(
                {
                    "opening_id": device.device_id,
                    "label": device.label or device.ring_name,
                    "orientation": device.orientation,
                    "state": state,
                    "since": since,
                    "minutes": round((now - since) / 60.0, 1) if since else None,
                }
            )

        view = self.view()
        rate = view.activity.rate(now - 1800.0, now)
        cameras = self.store.devices(role="street_camera")
        camera_online = all(c.online for c in cameras) if cameras else False

        return {
            "now": now,
            "synthetic_history": bool(self.store.get_meta("seeded_synthetic", False)),
            "sealed_floor": round(view.model.baseline.recent_floor, 2),
            "co_alert_ppm": CO_ALERT_PPM,
            "pm25_alert_ug_m3": PM25_ALERT_UG_M3,
            "indoor": {
                "pm25": sample["pm25"] if sample else None,
                "co": sample["co"] if sample else None,
                "temperature": sample["temperature"] if sample else None,
                "humidity": sample["humidity"] if sample else None,
                "reported_at": sample["reported_at"] if sample else None,
                "age_minutes": round(age_min, 1) if age_min is not None else None,
                "fresh": bool(age_min is not None and age_min <= AQM_MAX_SAMPLE_AGE_MIN),
                "pm25_alert": bool(sample and (sample["pm25"] or 0) > PM25_ALERT_UG_M3),
                "co_alert": bool(sample and (sample["co"] or 0) > CO_ALERT_PPM),
            },
            "street": {
                "events_last_30min": int(rate * 30),
                "rate_per_min": round(rate, 3),
                "percentile": round(view.activity.percentile(rate), 0),
                "band": view.activity.band(rate),
                "camera_online": camera_online,
                "total_events": view.activity.n_events,
                "total_bursts": view.activity.n_bursts,
            },
            "openings": openings,
        }

    def model_summary(self) -> dict[str, Any]:
        view = self.view()
        lo, hi = view.activity.spread()
        return {
            "model": view.model.as_dict(),
            "sources": view.source_report.as_dict(),
            "comparisons": [c.as_dict() for c in view.comparisons],
            "calibration": view.calibration.as_dict(),
            "street_spread": [round(lo, 2), round(hi, 2)],
            "n_intervals": len(view.intervals),
            "history_days": self._history_days(),
        }

    def _history_days(self) -> float:
        samples = self.store.aq_samples()
        if len(samples) < 2:
            return 0.0
        return round((samples[-1]["reported_at"] - samples[0]["reported_at"]) / 86400.0, 1)

    def recommendation(self, *, explore: bool = True) -> dict[str, Any]:
        view = self.view()
        sample = self.store.latest_aq_sample()
        current_pm = (sample["pm25"] if sample else None) or view.model.baseline.recent_floor
        rec = planner.recommend(
            view.model,
            view.activity,
            view.intervals,
            now=self.now(),
            current_pm=float(current_pm),
            humidity=sample["humidity"] if sample else None,
            co=sample["co"] if sample else None,
            draws=view.draws,
            explore=explore,
            interval_k=view.calibration.k,
        )
        out = rec.as_dict()
        out["calibration"] = view.calibration.as_dict()
        return out

    def offer(self) -> dict[str, Any] | None:
        """Publish the current best plan into the ledger, so it can be scored."""
        rec = self.recommendation()
        best = rec.get("best")
        if not best:
            return None
        existing = [
            p
            for p in self.store.plans()
            if p["status"] == "offered" and abs(p["start_at"] - best["start_at"]) < 900
        ]
        if existing:
            return existing[0]
        row = {
            "plan_id": best["plan_id"] or uuid.uuid4().hex[:12],
            "created_at": self.now(),
            "opening_id": best["opening_id"],
            "opening_label": best["opening_label"],
            "start_at": best["start_at"],
            "duration_min": best["duration_min"],
            "target_exchanges": rec["demand"]["target_exchanges"],
            "pred_exchanges": best["pred_exchanges"],
            "pred_intake": best["pred_intake"],
            "pred_intake_lo": best["pred_intake_lo"],
            "pred_intake_hi": best["pred_intake_hi"],
            "baseline_intake": (rec.get("habit") or {}).get("pred_intake"),
            "status": "offered",
            "detail": rec["reason"],
        }
        self.store.add_plan(row)
        return row

    def settle_plans(self) -> list[dict[str, Any]]:
        """Score every plan whose window has passed."""
        view = self.view()
        now = self.now()
        settled = []
        for row in self.store.plans():
            if row["status"] not in ("offered", "executed"):
                continue
            if row["start_at"] + (row["duration_min"] + planner.RECOVERY_MIN) * 60.0 > now:
                continue
            result = planner.verify(view.model, view.intervals, row)
            if result is None:
                continue
            self.store.update_plan(
                row["plan_id"],
                status="verified",
                realized_intake=result.realised_intake,
                realized_exchanges=result.realised_exchanges,
                verdict=result.verdict,
                detail=result.detail,
            )
            settled.append(result.as_dict())
        return settled

    def ledger(self) -> dict[str, Any]:
        rows = self.store.plans()
        scorable = ("inside_interval", "above", "below")
        verified = [r for r in rows if r["status"] == "verified" and r["verdict"] in scorable]
        inside = [r for r in verified if r["verdict"] == "inside_interval"]
        return {
            "plans": rows[:40],
            "n_total": len(rows),
            "n_verified": len(verified),
            "n_not_followed": sum(1 for r in rows if r["verdict"] == "not_followed"),
            "n_inside_interval": len(inside),
            "coverage": round(len(inside) / len(verified), 3) if verified else None,
            "backtest": self.backtest().as_dict(),
        }

    #: How much a record has to grow before the held-out replay is worth
    #: running again. The replay is over months of a household's life and three
    #: more readings do not move it, but it costs three fits and a calibration,
    #: so recomputing it on every arriving webhook makes the Record tab take
    #: fifteen seconds to answer for a number that has not changed.
    BACKTEST_REFRESH_FRACTION = 0.01

    def backtest(self) -> holdout.Backtest:
        """The held-out replay, cached until the record has meaningfully grown."""
        stamp = self._fingerprint()
        if self._backtest is not None:
            grown = sum(stamp) - sum(self._backtest_stamp)
            if grown < max(sum(self._backtest_stamp), 1) * self.BACKTEST_REFRESH_FRACTION:
                return self._backtest
        view = self.view()
        openings = [
            (d.device_id, d.label or d.ring_name, d.orientation)
            for d in self.store.devices(role="opening")
        ]
        self._backtest = holdout.run(view.intervals, openings)
        self._backtest_stamp = stamp
        return self._backtest

    def street_profile(self) -> dict[str, Any]:
        """The street's own weekly rhythm, in events per minute.

        This is the object the planner forecasts from, so it is the one the UI
        shows. It is a forecast of this street's habit, not of the street: a
        road closure or a delivery surge will not be in it.
        """
        view = self.view()
        index = view.activity
        now = self.now()
        stamp = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
        slot_now = (stamp.hour * 60 + stamp.minute) // activity_mod.PROFILE_SLOT_MIN
        return {
            "slot_minutes": activity_mod.PROFILE_SLOT_MIN,
            "weekday": [round(float(v), 4) for v in index.weekday_profile],
            "weekend": [round(float(v), 4) for v in index.weekend_profile],
            "now_slot": int(slot_now),
            "now_is_weekday": stamp.weekday() < 5,
            "n_events": index.n_events,
            "n_bursts": index.n_bursts,
        }

    # --- timeline ---------------------------------------------------------
    def timeline(self, hours: float = 24.0) -> dict[str, Any]:
        now = self.now()
        start = now - hours * 3600.0
        view = self.view()

        samples = [
            {
                "t": s["reported_at"],
                "pm25": s["pm25"],
                "humidity": s["humidity"],
                "temperature": s["temperature"],
                "co": s["co"],
            }
            for s in self.store.aq_samples(start=start)
        ]

        slot = 900.0
        n_slots = int(hours * 3600.0 / slot)
        street = []
        for i in range(n_slots):
            a = start + i * slot
            street.append(
                {
                    "t": a,
                    "events": int(view.activity.rate(a, a + slot) * (slot / 60.0)),
                    "rate": round(view.activity.rate(a, a + slot), 3),
                }
            )

        openings = []
        for device in self.store.devices(role="opening"):
            transitions = intervals_mod.open_state_series(self.store, [device.device_id])[
                device.device_id
            ]
            spans = []
            open_at: float | None = None
            for t, state in transitions:
                if state == "open" and open_at is None:
                    open_at = t
                elif state == "closed" and open_at is not None:
                    if t > start:
                        spans.append({"from": max(open_at, start), "to": t})
                    open_at = None
            if open_at is not None:
                spans.append({"from": max(open_at, start), "to": now})
            openings.append(
                {
                    "opening_id": device.device_id,
                    "label": device.label or device.ring_name,
                    "spans": spans,
                }
            )

        flagged = [
            {"from": iv.t0, "to": iv.t1}
            for iv in view.intervals
            if iv.indoor_source and iv.t1 >= start
        ]

        return {
            "start": start,
            "end": now,
            "samples": samples,
            "street": street,
            "openings": openings,
            "indoor_source_spans": flagged,
            "events": self._recent_events(start),
        }

    def _recent_events(self, start: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        labels = {
            d.device_id: (d.label or d.ring_name) for d in self.store.devices(role="opening")
        }
        for device_id, at, state in self.store.contact_events():
            if at >= start:
                out.append(
                    {
                        "t": at,
                        "kind": "opening",
                        "text": f"{labels.get(device_id, device_id)} {state}",
                    }
                )
        for row in self.store.threshold_events():
            if row["event_time"] >= start:
                out.append(
                    {
                        "t": row["event_time"],
                        "kind": "threshold",
                        "text": f"{row['event_type']}"
                        + (f" at {row['level']:.0f} ug/m3" if row["level"] else ""),
                    }
                )
        out.sort(key=lambda e: e["t"], reverse=True)
        return out[:40]

    # --- setup health -----------------------------------------------------
    def health(self) -> dict[str, Any]:
        view = self.view()
        now = self.now()
        sample = self.store.latest_aq_sample()
        checks = []

        cameras = self.store.devices(role="street_camera")
        checks.append(
            {
                "name": "Outdoor camera linked",
                "ok": bool(cameras),
                "detail": cameras[0].ring_name if cameras else "no camera mapped",
            }
        )
        recent_vehicle = len(view.activity.times) and (now - view.activity.times[-1]) < 6 * 3600
        checks.append(
            {
                "name": "Vehicle-motion events arriving",
                "ok": bool(recent_vehicle),
                "detail": (
                    f"{view.activity.n_events} events recorded"
                    if recent_vehicle
                    else "no vehicle-motion events recently. Check that the camera's "
                    "motion zone in the Ring app covers the street."
                ),
            }
        )
        openings = self.store.devices(role="opening")
        checks.append(
            {
                "name": "Openings mapped to contact sensors",
                "ok": len(openings) >= 2,
                "detail": f"{len(openings)} mapped"
                + ("" if len(openings) >= 2 else "; at least two are needed to compare"),
            }
        )
        age = (now - sample["reported_at"]) / 60.0 if sample else None
        checks.append(
            {
                "name": "Air Quality Monitor reporting",
                "ok": bool(age is not None and age <= AQM_MAX_SAMPLE_AGE_MIN),
                "detail": (
                    f"last reading {age:.0f} min ago" if age is not None else "no readings yet"
                ),
            }
        )
        checks.append(
            {
                "name": "Model ready",
                "ok": bool(view.model.reported_openings()),
                "detail": (
                    f"{len(view.model.reported_openings())} of {len(view.model.openings)} "
                    "openings estimated"
                ),
            }
        )
        return {"checks": checks, "all_ok": all(c["ok"] for c in checks)}


def local_time(t: float) -> str:
    return dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%H:%M")
