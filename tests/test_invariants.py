"""Property tests over full replays: the Part 4 lossless-score claim and the
reconstruction semantics of the two structural arms."""

from __future__ import annotations

import pytest

from provlab.axes import BASE_AXES
from conftest import run_once

SEEDS = list(range(8))


@pytest.mark.parametrize("seed", SEEDS)
def test_lossless_score_invariant(seed: int) -> None:
    """Structural arm base-axis scores == oracle at every decision point.

    By construction, not a discovery: compaction never touches the four base
    axes, and the running min is constant-size."""
    result = run_once(seed)
    oracle = {
        (r.step, r.policy): r.scores
        for r in result.records
        if r.arm == "ground_truth" and r.mode == "blind"
    }
    checked = 0
    for r in result.records:
        if r.arm not in ("structural_min", "structural_perhop") or r.mode != "blind":
            continue
        expected = oracle[(r.step, r.policy)]
        for axis in BASE_AXES:
            assert r.scores[axis] == expected[axis], (seed, r.step, r.arm, axis)
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_score_gates_agree_100_percent_with_structural_arms(seed: int) -> None:
    """H1 sanity class: score gates never consult reconstruction, so they must
    agree perfectly between the oracle and both structural arms."""
    result = run_once(seed)
    oracle = {
        (r.step, r.policy): r.proceed
        for r in result.records
        if r.arm == "ground_truth" and r.mode == "blind"
    }
    for r in result.records:
        if r.gate_class != "score" or r.mode != "blind":
            continue
        if r.arm in ("structural_min", "structural_perhop"):
            assert r.proceed == oracle[(r.step, r.policy)]


def test_reconstruction_decays_as_running_min_power() -> None:
    """structural_min: reconstruction == (1 - penalty) ** n_compactions."""
    result = run_once(seed=0, steps=300, cadence=10, penalty=0.02)
    for point in result.recon_curve:
        assert point.recon_min == pytest.approx(0.98**point.n_compactions)
    # monotone non-increasing (Boyko's recursion)
    values = [p.recon_min for p in result.recon_curve]
    assert all(a >= b for a, b in zip(values, values[1:]))


def test_perhop_fidelity_does_not_decay_with_cycle_count() -> None:
    """structural_perhop: fidelity = worst single penalty, independent of how
    many compaction cycles happened."""
    result = run_once(seed=0, steps=300, cadence=10, penalty=0.02)
    late = [p for p in result.recon_curve if p.n_compactions >= 1]
    assert late, "expected at least one compaction"
    assert all(p.fidelity_perhop == pytest.approx(0.98) for p in late)
