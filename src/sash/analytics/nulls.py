"""Before SASH names a better window, the label has to earn it.

Two openings in the same flat produce two different numbers even when they are
physically identical, because the estimates are noisy and the household used
them on different days. A product that announces "your courtyard window is
better" on that basis is telling a story about noise.

The test here is the obvious one: hold every episode exactly as it happened and
shuffle only which opening it belonged to. Under the null that the two openings
behave the same, that shuffle changes nothing real, so the spread of the
shuffled differences is the spread the estimator produces from nothing. The
observed difference has to sit outside it.

The shuffle keeps whole episodes together, because an episode is one airing, and
it keeps the two group sizes fixed, because a difference between a 40-reading
opening and a 12-reading opening is partly a difference in precision. The
quantity shuffled is the one the recommendation is made on: the street coupling
out of the joint fit, with the same shared background under both halves.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .estimator import BaselineFit, fit_incoming, isolated_intervals
from .intervals import Interval


@dataclass
class Comparison:
    a_id: str
    b_id: str
    a_label: str
    b_label: str
    observed_coupling_gap: float
    p_value: float
    n_permutations: int
    n_episodes_a: int
    n_episodes_b: int
    significant: bool
    verdict: str

    def as_dict(self) -> dict:
        return {
            "a_id": self.a_id,
            "b_id": self.b_id,
            "a_label": self.a_label,
            "b_label": self.b_label,
            "coupling_gap": round(self.observed_coupling_gap, 3),
            "p_value": round(self.p_value, 4),
            "n_permutations": self.n_permutations,
            "n_episodes_a": self.n_episodes_a,
            "n_episodes_b": self.n_episodes_b,
            "significant": self.significant,
            "verdict": self.verdict,
        }


def _episode_blocks(rows: Sequence[Interval]) -> dict[str, list[Interval]]:
    blocks: dict[str, list[Interval]] = {}
    for iv in rows:
        blocks.setdefault(iv.episode_id or f"solo:{iv.t0}", []).append(iv)
    return blocks


def compare(
    intervals: Sequence[Interval],
    a_id: str,
    b_id: str,
    labels: dict[str, str],
    baseline: BaselineFit,
    exchange: dict[str, float],
    all_ids: Sequence[str],
    *,
    permutations: int = 400,
    alpha: float = 0.05,
    seed: int = 5,
) -> Comparison:
    """Permutation test on the street-coupling gap between two openings.

    The exchange rates come from the CO tracer, which is a different data path
    from the one being shuffled, so they are held fixed. What is shuffled is the
    only thing the claim rests on: which opening each airing belonged to.
    """
    rows_a = isolated_intervals(intervals, a_id, all_ids)
    rows_b = isolated_intervals(intervals, b_id, all_ids)
    blocks_a = _episode_blocks(rows_a)
    blocks_b = _episode_blocks(rows_b)

    theta_a = exchange.get(a_id, 0.0)
    theta_b = exchange.get(b_id, 0.0)
    thetas = {a_id: theta_a, b_id: theta_b}

    def gap(left: Sequence[Interval], right: Sequence[Interval]) -> float:
        _bg, gs, _ref = fit_incoming(
            {a_id: list(left), b_id: list(right)}, baseline, thetas
        )
        return gs.get(a_id, 0.0) - gs.get(b_id, 0.0)

    observed = gap(rows_a, rows_b)

    n_a, n_b = len(blocks_a), len(blocks_b)
    if n_a < 4 or n_b < 4:
        return Comparison(
            a_id,
            b_id,
            labels.get(a_id, a_id),
            labels.get(b_id, b_id),
            observed,
            1.0,
            0,
            n_a,
            n_b,
            False,
            "not enough separate airings to compare these two openings",
        )

    pooled = list(blocks_a.values()) + list(blocks_b.values())
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(permutations):
        order = rng.permutation(len(pooled))
        left = [iv for i in order[:n_a] for iv in pooled[i]]
        right = [iv for i in order[n_a:] for iv in pooled[i]]
        # Both halves are fitted under each opening's own identity, in the same
        # joint fit with the same shared background, so the design matrix is
        # built exactly the way it was for the observed value.
        shuffled = gap(_relabel(left, a_id), _relabel(right, b_id))
        if abs(shuffled) >= abs(observed):
            exceed += 1

    p_value = (exceed + 1) / (permutations + 1)
    significant = p_value < alpha
    if significant:
        cheaper = labels.get(b_id if observed > 0 else a_id, "the other opening")
        verdict = f"{cheaper} admits measurably less street-linked particle load"
    else:
        verdict = "these two openings are not yet distinguishable"

    return Comparison(
        a_id,
        b_id,
        labels.get(a_id, a_id),
        labels.get(b_id, b_id),
        observed,
        p_value,
        permutations,
        n_a,
        n_b,
        significant,
        verdict,
    )


def _relabel(rows: Sequence[Interval], target_id: str) -> list[Interval]:
    """Re-key each interval's open fraction onto ``target_id``.

    A permuted episode has to be fitted as though it belonged to the opening it
    was assigned, which means moving whatever open fraction it carries onto that
    opening's key. Everything else about the interval is left alone.
    """
    out: list[Interval] = []
    for iv in rows:
        frac = max(iv.open_frac.values()) if iv.open_frac else 0.0
        out.append(dataclasses.replace(iv, open_frac={target_id: frac}))
    return out


def rank_openings(
    intervals: Sequence[Interval],
    model,
    *,
    permutations: int = 400,
    seed: int = 5,
) -> list[Comparison]:
    """Compare the best-looking opening against every other reported one."""
    reported = [o for o in model.openings.values() if o.reported]
    if len(reported) < 2:
        return []
    labels = {o.opening_id: o.label for o in model.openings.values()}
    exchange = {o.opening_id: o.exchange for o in model.openings.values()}
    all_ids = list(model.openings)
    best = min(reported, key=lambda o: o.coupling)
    out = []
    for other in reported:
        if other.opening_id == best.opening_id:
            continue
        out.append(
            compare(
                intervals,
                other.opening_id,
                best.opening_id,
                labels,
                model.baseline,
                exchange,
                all_ids,
                permutations=permutations,
                seed=seed,
            )
        )
    return out
