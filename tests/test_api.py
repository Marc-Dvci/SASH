"""The HTTP surface, driven the way the browser and Ring drive it.

Two halves that must not touch. The Ring half acknowledges inside five seconds
and does no analysis; the product half reads one cached household view and never
touches the network. The tests below are the walkthrough a person would do by
hand, in order: link, deliver, look, decide, publish, score.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sash.api import create_app
from sash.config import SashConfig
from sash.ring.webhook import sign
from sash.service import Household
from sash.store import Store
from simulator.ring_api import contact_payload, motion_payload
from simulator.seed import build_home, seed_store

HMAC_KEY = "test-signing-key"
TAPE_END = 1_760_000_000.0


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    home = build_home(days=45.0, seed=7, end=TAPE_END)
    path = tmp_path_factory.mktemp("api") / "api.sqlite"
    store = Store(str(path))
    asyncio.run(seed_store(store, home, hmac_key=HMAC_KEY, duplicate_every=0))

    config = SashConfig()
    config.ring.hmac_key = HMAC_KEY
    household = Household(store, clock=lambda: TAPE_END)
    app = create_app(store, config=config, household=household)
    with TestClient(app) as c:
        yield c
    store.close()


def _post_event(client, payload):
    raw = json.dumps(payload).encode()
    return client.post(
        "/ring/webhook", content=raw, headers={"X-Signature": sign(raw, HMAC_KEY)}
    )


# --- the Ring half ----------------------------------------------------------


def test_a_signed_webhook_is_accepted(client):
    r = _post_event(client, motion_payload("ava1.ring.device.SIMCAMFRONT1A2B", TAPE_END + 1, "vehicle"))
    assert r.status_code == 200 and r.json()["ok"]


def test_an_unsigned_webhook_gets_401_not_200(client):
    raw = json.dumps(motion_payload("ava1.ring.device.SIMCAMFRONT1A2B", TAPE_END + 2, "vehicle")).encode()
    r = client.post("/ring/webhook", content=raw, headers={"X-Signature": "sha256=00"})
    assert r.status_code == 401


def test_a_replayed_delivery_still_gets_200(client):
    """From Ring's side a retry that is acknowledged is a retry that stops."""
    payload = contact_payload("ava1.ring.device.SIMWINCOURT4M8D", TAPE_END + 3, True)
    first = _post_event(client, payload)
    second = _post_event(client, payload)
    assert first.status_code == 200 and second.status_code == 200
    assert "duplicate" in second.json()["reason"]


def test_account_linking_runs_from_link_to_discovered_devices():
    """``/ring/link`` to Ring's consent page, ``/ring/callback`` exchanges the code."""
    from urllib.parse import parse_qs, urlparse

    import httpx

    from sash.ingest import IngestStats
    from sash.live import LiveRing
    from simulator.ring_api import create_app as create_ring_app

    home = build_home(days=2, seed=4)
    home.clock = home.result.t0 + 86400.0
    ring_app = create_ring_app(home, HMAC_KEY)
    config = SashConfig()
    config.ring.api_base_url = "http://ring"
    config.ring.oauth_token_url = "http://ring/oauth/token"
    config.ring.client_id = "sash-client"
    config.ring.client_secret = "sash-secret"
    config.ring.redirect_uri = "http://testserver/ring/callback"
    config.ring.access_token = ""
    store = Store(":memory:")
    live = LiveRing(config, store, IngestStats(), transport=httpx.ASGITransport(app=ring_app))
    live.oauth._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ring_app))
    app = create_app(store, config=config, household=Household(store), ring=live)

    with TestClient(app) as c:
        assert c.get("/api/ring").json()["linked"] is False
        r = c.get("/ring/link", follow_redirects=False)
        assert r.status_code == 302
        target = urlparse(r.headers["location"])
        assert target.netloc == "account.ring.com"
        query = parse_qs(target.query)
        assert query["client_id"] == ["sash-client"] and query["scope"] == ["ava"]

        forged = c.get("/ring/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
        assert forged.status_code == 400

        r = c.get(
            "/ring/callback",
            params={"code": "c0de", "state": query["state"][0]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        ring = c.get("/api/ring").json()
        assert ring["linked"] is True and ring["account_id"] == home.account_id
        assert ring["devices"] == 2 + len(home.labels)


def test_the_callback_reports_an_error(client):
    r = client.get("/ring/callback", params={"error": "access_denied"})
    assert r.status_code == 400


def test_ring_stats_counts_what_the_front_door_saw(client):
    body = client.get("/ring/stats").json()
    assert body["webhook"]["received"] >= 3
    assert body["webhook"]["invalid_signature"] >= 1
    assert body["webhook"]["duplicate"] >= 1


# --- the product half -------------------------------------------------------


def test_healthz_reports_the_demo_controls_are_absent_here(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["demo_controls"] is False


def test_status_carries_a_reading_and_every_opening(client):
    body = client.get("/api/status").json()
    assert body["indoor"]["pm25"] is not None
    assert len(body["openings"]) == 3
    assert {o["state"] for o in body["openings"]} <= {"open", "closed"}


def test_the_model_reports_openings_with_intervals_on_both_estimates(client):
    body = client.get("/api/model").json()
    openings = body["model"]["openings"]
    assert len(openings) == 3
    for est in openings.values():
        assert est["reported"] or est["reason"]
        if est["reported"]:
            assert est["exchange_ci"][0] <= est["exchange"] <= est["exchange_ci"][1]
            assert est["coupling_ci"][0] <= est["coupling"] <= est["coupling_ci"][1]


def test_every_comparison_carries_a_p_value_and_a_sentence(client):
    for c in client.get("/api/model").json()["comparisons"]:
        assert 0.0 < c["p_value"] <= 1.0
        assert c["verdict"]
        assert c["significant"] == (c["p_value"] < 0.05)


def test_the_recommendation_names_an_opening_a_time_and_a_cost(client):
    body = client.get("/api/recommendation").json()
    best = body["best"]
    assert best is not None
    assert best["opening_label"]
    assert best["start_at"] >= TAPE_END
    assert best["pred_intake_lo"] <= best["pred_intake"] <= best["pred_intake_hi"]
    assert body["narrative"]["text"]
    assert body["narrative"]["source"] in ("deterministic", "bedrock")


def test_the_habit_is_a_plan_the_household_would_have_made(client):
    body = client.get("/api/recommendation").json()
    assert body["habit"] is not None
    assert body["habit"]["opening_label"]


def test_the_timeline_covers_the_hours_it_was_asked_for(client):
    body = client.get("/api/timeline?hours=24").json()
    assert body["end"] - body["start"] == pytest.approx(24 * 3600.0, rel=0.01)
    assert body["samples"]
    assert "openings" in body and "street" in body


@pytest.mark.parametrize("hours", [0.5, 400])
def test_the_timeline_refuses_a_window_outside_its_range(client, hours):
    assert client.get(f"/api/timeline?hours={hours}").status_code == 422


def test_the_activity_profile_has_a_slot_for_every_half_hour(client):
    body = client.get("/api/activity").json()
    assert len(body["weekday"]) == 48 and len(body["weekend"]) == 48
    assert 0 <= body["now_slot"] < 48


def test_health_checks_are_sentences_a_person_can_act_on(client):
    for check in client.get("/api/health").json()["checks"]:
        assert check["name"] and check["detail"]


# --- publishing and scoring -------------------------------------------------


def test_publishing_a_plan_writes_it_down(client):
    row = client.post("/api/plan").json()
    assert row["status"] == "offered"
    assert row["pred_intake_lo"] <= row["pred_intake"] <= row["pred_intake_hi"]
    ledger = client.get("/api/ledger").json()
    assert any(p["plan_id"] == row["plan_id"] for p in ledger["plans"])


def test_publishing_twice_does_not_duplicate_the_same_plan(client):
    first = client.post("/api/plan").json()
    second = client.post("/api/plan").json()
    assert first["plan_id"] == second["plan_id"]


def test_the_ledger_carries_the_held_out_backtest(client):
    back = client.get("/api/ledger").json()["backtest"]
    assert back["fit_days"] > 0
    assert back["n_airings"] >= back["n_scored"]
    if back["n_scored"]:
        assert 0.0 <= back["coverage"] <= 1.0


def test_the_demo_routes_are_absent_without_the_simulator(client):
    assert client.post("/api/demo/advance").status_code == 404
    assert client.post("/api/demo/follow").status_code == 404


# --- the device mapping -----------------------------------------------------


def test_remapping_a_device_changes_what_the_model_is_about(client):
    before = client.get("/api/model").json()["model"]["openings"]
    assert "ava1.ring.device.SIMWINCOURT4M8D" in before
    r = client.post(
        "/api/devices/ava1.ring.device.SIMWINCOURT4M8D/role",
        json={"app_role": "unused", "label": "Courtyard window", "orientation": None},
    )
    assert r.status_code == 200
    after = client.get("/api/model").json()["model"]["openings"]
    assert "ava1.ring.device.SIMWINCOURT4M8D" not in after
    # Put it back, so the rest of the module sees the household it expects.
    client.post(
        "/api/devices/ava1.ring.device.SIMWINCOURT4M8D/role",
        json={"app_role": "opening", "label": "Courtyard window", "orientation": "courtyard"},
    )


def test_remapping_an_unknown_device_is_a_404(client):
    r = client.post("/api/devices/nope/role", json={"app_role": "opening"})
    assert r.status_code == 404


# --- the UI is served -------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/app.css", "/app.js"])
def test_the_ui_is_served(client, path):
    assert client.get(path).status_code == 200


# --- against a Ring account -------------------------------------------------


def test_live_discovers_and_polls_through_the_documented_endpoints():
    """``sash live`` with a Playground token: discovery, roles, one poll."""
    import httpx

    from sash.config import SashConfig
    from sash.ingest import IngestStats
    from sash.live import LiveRing, spike
    from simulator.ring_api import create_app as create_ring_app
    from simulator.seed import build_home

    home = build_home(days=2, seed=3)
    home.clock = home.result.t0 + 86400.0
    ring_app = create_ring_app(home, "sash-dev-key")
    config = SashConfig()
    config.ring.api_base_url = "http://ring"
    config.ring.access_token = "playground-token"
    store = Store(":memory:")

    report = asyncio.run(spike(config, store, transport=httpx.ASGITransport(app=ring_app)))
    assert report["mode"] == "access_token"
    assert report["account"] == home.account_id
    assert {d["family"] for d in report["devices"]} == {
        "camera", "air_quality_monitor", "contact_sensor"
    }
    assert report["poll"]["new_samples_first_pass"] == 1
    assert report["poll"]["new_samples_second_pass"] == 0
    assert report["poll"]["repeats_refused"] == 1
    assert report["rate_limit"]["limit"] == 100

    roles = {d.device_id: d.app_role for d in store.devices()}
    assert roles[home.aqm_id] == "indoor_air"
    assert roles[home.camera_id] == "street_camera"
    assert all(roles[o] == "opening" for o in home.labels)

    live = LiveRing(config, store, IngestStats(), transport=httpx.ASGITransport(app=ring_app))
    assert live.linked and live.describe()["mode"] == "access_token"
    asyncio.run(live.stop())
