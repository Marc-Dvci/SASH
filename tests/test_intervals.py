"""The observation table: alignment, open fractions, outages, episodes.

Two of these are the difference between a model and an artefact.

An interval whose covariate window overlaps a camera outage has no street
measurement, only an absence of one. Treating that absence as a quiet street is
the single easiest way to invent a coupling, so those intervals carry
``camera_online = False`` and every street term drops them.

And a contact sensor's state before the first transition SASH ever saw is not
"closed", it is unknown. SASH takes it as closed and does not invent history
before the account was linked, because Ring has no event-history endpoint for
sensors and there is nothing to look it up in.
"""

from __future__ import annotations

import pytest

from sash.analytics import intervals as iv_mod
from sash.analytics.intervals import Interval, open_fraction


def _t(minutes: float) -> float:
    return 1_760_000_000.0 + minutes * 60.0


def test_open_fraction_of_a_window_open_the_whole_time():
    transitions = [(_t(0), "open"), (_t(60), "closed")]
    assert open_fraction(transitions, _t(15), _t(45)) == pytest.approx(1.0)


def test_open_fraction_of_a_window_open_for_half_of_it():
    transitions = [(_t(30), "open"), (_t(60), "closed")]
    assert open_fraction(transitions, _t(15), _t(45)) == pytest.approx(0.5)


def test_open_fraction_before_the_first_transition_is_closed():
    """SASH has no history before the account was linked and invents none."""
    transitions = [(_t(100), "open"), (_t(130), "closed")]
    assert open_fraction(transitions, _t(0), _t(60)) == 0.0


def test_open_fraction_of_a_window_still_open_at_the_end():
    transitions = [(_t(0), "open")]
    assert open_fraction(transitions, _t(30), _t(90)) == pytest.approx(1.0)


def test_open_fraction_is_zero_for_a_zero_length_window():
    assert open_fraction([(_t(0), "open")], _t(10), _t(10)) == 0.0


def test_episode_ids_group_one_airing_together():
    transitions = [
        (_t(0), "open"),
        (_t(40), "closed"),
        (_t(600), "open"),
        (_t(650), "closed"),
    ]
    episodes = iv_mod._episode_ids(transitions, "win")
    assert len(episodes) == 2
    assert episodes[0][2] != episodes[1][2]


def test_an_unterminated_episode_runs_to_the_end():
    episodes = iv_mod._episode_ids([(_t(0), "open")], "win")
    assert len(episodes) == 1 and episodes[0][1] == float("inf")


# --- through the store ------------------------------------------------------


def _seed_store(store, *, offline=None):
    store.upsert_device("aqm", "Air", "air_quality_monitor")
    store.set_role("aqm", "indoor_air", "Air", None)
    store.upsert_device("cam", "Front", "camera")
    store.set_role("cam", "street_camera", "Front", "street")
    store.upsert_device("win", "Window", "contact_sensor")
    store.set_role("win", "opening", "Window", "street")

    for i in range(12):
        store.add_aq_sample("aqm", _t(15 * i), _t(15 * i) + 30, 5.0 + i, 0.1, 20.0, 45.0)
    store.add_contact_event("e1", "win", _t(30), "open")
    store.add_contact_event("e2", "win", _t(60), "closed")
    for i in range(40):
        store.add_vehicle_event(f"v{i}", "cam", _t(4 * i), "webhook")
    if offline:
        store.set_online("cam", False, offline[0])
        store.set_online("cam", True, offline[1])


def test_intervals_are_built_from_consecutive_readings(store):
    _seed_store(store)
    rows = iv_mod.build(store)
    assert len(rows) == 11
    assert all(r.dt_h == pytest.approx(0.25) for r in rows)


def test_the_open_window_lands_on_the_right_intervals(store):
    _seed_store(store)
    rows = iv_mod.build(store)
    fracs = [r.open_frac["win"] for r in rows]
    assert fracs[0] == 0.0
    assert fracs[2] == pytest.approx(1.0)
    assert sum(fracs) == pytest.approx(2.0)


def test_intervals_overlapping_a_camera_outage_are_marked(store):
    """An offline camera reports no vehicles. That is not a quiet street."""
    _seed_store(store, offline=(_t(70), _t(110)))
    rows = iv_mod.build(store)
    offline_rows = [r for r in rows if not r.camera_online]
    assert offline_rows, "the outage must reach the table"
    assert all(r.t0 < _t(110) and r.t1 > _t(70) for r in offline_rows)


def test_a_reading_fetched_far_too_late_cannot_anchor_an_interval(store):
    store.upsert_device("aqm", "Air", "air_quality_monitor")
    store.set_role("aqm", "indoor_air", "Air", None)
    store.add_aq_sample("aqm", _t(0), _t(0) + 30, 5.0, 0.1, 20.0, 45.0)
    store.add_aq_sample("aqm", _t(15), _t(15) + 30, 6.0, 0.1, 20.0, 45.0)
    # Fetched two hours after it was reported: the covariate window it would
    # anchor is not the window the reading describes.
    store.add_aq_sample("aqm", _t(30), _t(30) + 7200, 7.0, 0.1, 20.0, 45.0)
    rows = iv_mod.build(store)
    assert len(rows) == 1


def test_no_air_quality_monitor_means_no_table(store):
    store.upsert_device("cam", "Front", "camera")
    assert iv_mod.build(store) == []


def test_rate_is_the_change_per_hour():
    row = Interval(t0=0.0, t1=900.0, dt_h=0.25, c0=10.0, c1=13.0, activity=0.0)
    assert row.rate == pytest.approx(12.0)


def test_any_open_ignores_a_sliver():
    row = Interval(t0=0.0, t1=900.0, dt_h=0.25, c0=1.0, c1=1.0, activity=0.0,
                   open_frac={"w": 0.01})
    assert not row.any_open
    row.open_frac["w"] = 0.3
    assert row.any_open
