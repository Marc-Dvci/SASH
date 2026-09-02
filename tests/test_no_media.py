"""SASH cannot read a camera's imagery, and this is where that is enforced.

Ring's media endpoints sit behind the same Cameras and Doorbells scope that
delivers ``motion.vehicle`` events, so the grant a household gives SASH is
wider than the product needs. A promise in a README is not a control. The
control is a path allowlist in the client, and the test that matters is the one
run against a server that *would* have answered: the simulator implements all
three media endpoints and returns a body, so if the refusal ever stops working
these tests go green in a way that is visible.
"""

from __future__ import annotations

import httpx
import pytest

from sash.config import RingConfig
from sash.ring.auth import StaticTokenService
from sash.ring.client import (
    ALLOWED_PATHS,
    MediaAccessDenied,
    PathNotAllowed,
    RingClient,
    check_path,
)
from simulator.ring_api import create_app
from simulator.seed import build_home

#: Every media path in Ring's documented surface, as the client would build it.
MEDIA_PATHS = [
    "/v1/devices/cam-1/media/video/download",
    "/v1/devices/cam-1/media/image/download",
    "/v1/devices/cam-1/media/streaming/whep/sessions",
    "/v1/devices/cam-1/media/audio/download",
    "/v1/media/video/download",
]


@pytest.fixture(scope="module")
def sim_client():
    home = build_home(days=2.0, seed=3, end=1_760_000_000.0)
    app = create_app(home, "k")
    config = RingConfig()
    config.api_base_url = "http://sim"
    tokens = StaticTokenService("t")
    return home, RingClient(config, tokens.bearer, transport=httpx.ASGITransport(app=app))


@pytest.mark.parametrize("path", MEDIA_PATHS)
def test_media_paths_are_refused_before_a_request_is_made(path):
    with pytest.raises(MediaAccessDenied):
        check_path(path)


@pytest.mark.parametrize("path", MEDIA_PATHS)
@pytest.mark.asyncio
async def test_the_simulator_would_answer_but_the_client_never_asks(sim_client, path):
    """The server is willing. That is the whole point of testing it this way."""
    home, client = sim_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(home, "k")), base_url="http://sim"
    ) as raw:
        served = await raw.post(path)
    # The three documented endpoints answer; the two invented ones 404. Either
    # way the server is not what stops SASH.
    assert served.status_code in (200, 404, 405)

    before = client.metrics.calls
    with pytest.raises(MediaAccessDenied):
        await client.request("POST", path)
    assert client.metrics.calls == before, "a refused path must not reach the transport"
    assert client.metrics.media_refusals >= 1


@pytest.mark.asyncio
async def test_the_convenience_methods_are_refused_too(sim_client):
    """These exist so the refusal is discoverable from the client's own surface."""
    _home, client = sim_client
    for call in (client.download_video, client.download_image, client.start_live_view):
        with pytest.raises(MediaAccessDenied):
            await call("cam-1")


def test_an_unlisted_path_is_refused_as_unlisted_not_as_media():
    with pytest.raises(PathNotAllowed):
        check_path("/v1/devices/cam-1/siren")


@pytest.mark.parametrize(
    "path",
    [
        "/v1/devices",
        "/v1/devices/aqm-1",
        "/v1/devices/aqm-1/status",
        "/v1/devices/aqm-1/capabilities",
        "/v1/history/devices/cam-1/events",
        "/v1/users/me",
    ],
)
def test_the_paths_sash_does_need_are_allowed(path):
    check_path(path)


def test_the_allowlist_is_small_enough_to_read_in_one_screen():
    """A blast radius nobody reads is not a control."""
    assert len(ALLOWED_PATHS) <= 12
