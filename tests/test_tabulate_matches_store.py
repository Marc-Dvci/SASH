"""The benchmark's shortcut has to agree with the product's own path.

``sash.analytics.intervals.build`` reads the store, so it is fed by webhooks and
status polls that have been through signature verification, idempotency and the
contact vocabulary. ``simulator.tabulate.intervals_from_sim`` skips all of that
for speed, and forty households at four months each is why. If the two ever
disagreed, every number in ``bench/results`` would be about a pipeline nobody
ships.

So they are pinned against each other here on a short record, through the real
receiver and the real client.
"""

from __future__ import annotations

import asyncio

import pytest

from sash.analytics.intervals import build
from sash.store import Store
from simulator.seed import build_home, seed_store
from simulator.tabulate import intervals_from_sim


@pytest.fixture(scope="module")
def both_tables(tmp_path_factory):
    home = build_home(days=6.0, seed=5, end=1_760_000_000.0)
    path = tmp_path_factory.mktemp("db") / "t.sqlite"
    store = Store(str(path))
    asyncio.run(seed_store(store, home, hmac_key="k", duplicate_every=0))
    through_ring = build(store)
    direct = intervals_from_sim(home.result)
    store.close()
    return through_ring, direct


def test_the_two_tables_are_the_same_length(both_tables):
    through_ring, direct = both_tables
    assert len(through_ring) > 100
    assert len(through_ring) == len(direct)


def test_every_row_agrees(both_tables):
    through_ring, direct = both_tables
    for a, b in zip(through_ring, direct):
        assert a.t0 == b.t0
        assert a.t1 == b.t1
        assert a.c0 == pytest.approx(b.c0)
        assert a.c1 == pytest.approx(b.c1)
        assert a.activity == pytest.approx(b.activity, abs=1e-9)
        assert a.camera_online == b.camera_online
        assert a.episode_id == b.episode_id
        assert set(a.open_frac) == set(b.open_frac)
        for oid, frac in a.open_frac.items():
            assert frac == pytest.approx(b.open_frac[oid], abs=1e-9)


def test_the_contact_vocabulary_survived_the_round_trip(both_tables):
    """If ``faulted`` had been read as closed, every open fraction would invert."""
    through_ring, _direct = both_tables
    assert any(row.any_open for row in through_ring)
    assert not all(row.any_open for row in through_ring)
