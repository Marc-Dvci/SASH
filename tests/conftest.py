"""Shared fixtures.

Both ``sash`` (under ``src``) and ``simulator`` (top level) have to be
importable; ``pyproject.toml`` puts both on ``pythonpath`` for pytest, and this
repeats it so the suite also runs when pytest is invoked from elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

HMAC_KEY = "test-signing-key"


@pytest.fixture()
def store(tmp_path):
    from sash.store import Store

    s = Store(str(tmp_path / "t.sqlite"))
    yield s
    s.close()


@pytest.fixture(scope="session")
def short_home():
    """A simulated household, shared by the analytics tests.

    Long enough that the reporting gate actually opens. A shorter record makes
    every test that needs two reported openings skip, and a suite that skips its
    own subject is worse than no suite. Built once per session.
    """
    import numpy as np

    from simulator import home as sim

    params = sim.default_home()
    result = sim.simulate(params, days=75.0, t0=1_760_000_000.0, seed=7)
    return params, result


@pytest.fixture(scope="session")
def short_rows(short_home):
    from sash.analytics import sources
    from simulator.tabulate import intervals_from_sim

    _params, result = short_home
    rows = intervals_from_sim(result)
    sources.detect(rows)
    return rows
