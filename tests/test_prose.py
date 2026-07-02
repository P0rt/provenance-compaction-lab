from __future__ import annotations

import numpy as np
import pytest

from provlab.axes import AXES, pristine_scores
from provlab.llm import MockProseChannel, _parse_extraction
from conftest import run_once


def channel(seed: int = 0, **kwargs: float) -> MockProseChannel:
    return MockProseChannel(rng=np.random.default_rng(seed), **kwargs)


def test_mock_channel_realized_recall_and_precision() -> None:
    ch = channel(seed=1, taint_recall=0.6, taint_precision=0.9)
    taints = frozenset(f"taint:tool_flaky:{i}" for i in range(50))
    kept_total = 0
    fab_total = 0
    for step in range(200):
        out = ch.compress(scores=pristine_scores(), taints=taints, step=step)
        kept_total += out.n_kept
        fab_total += out.n_fabricated
    recall = kept_total / (50 * 200)
    precision = kept_total / (kept_total + fab_total)
    assert recall == pytest.approx(0.6, abs=0.03)
    assert precision == pytest.approx(0.9, abs=0.03)


def test_mock_channel_score_noise_clipped() -> None:
    ch = channel(seed=2, sigma=0.5)
    for step in range(50):
        out = ch.compress(scores=pristine_scores(), taints=frozenset(), step=step)
        assert all(0.0 <= out.scores[a] <= 1.0 for a in AXES)


def test_mock_channel_parse_failure_worst_case() -> None:
    ch = channel(seed=3, parse_failure_rate=1.0)
    out = ch.compress(scores=pristine_scores(), taints=frozenset({"t"}), step=1)
    assert out.parse_failed
    assert all(out.scores[a] == 0.0 for a in AXES)
    assert out.taints == frozenset()


def test_extraction_parser_defensive() -> None:
    assert _parse_extraction("no json here") is None
    assert _parse_extraction('{"scores": "nope"}') is None
    ok = _parse_extraction(
        'noise {"scores": {"freshness": 0.5, "capability": 1, "tool_integrity": 1,'
        ' "verification": 1, "reconstruction": 2.0}, "taints": ["t1"]} trailing'
    )
    assert ok is not None
    scores, taints = ok
    assert scores["freshness"] == 0.5
    assert scores["reconstruction"] == 1.0  # clipped
    assert taints == frozenset({"t1"})


def test_prose_arm_replaces_vector_and_taints_in_replay() -> None:
    result = run_once(seed=5, steps=200, cadence=10)
    assert result.prose_stats.n_extractions > 0
    # the channel both forgets and fabricates
    assert result.prose_stats.realized_recall() < 1.0
    assert result.prose_stats.n_fabricated_taints > 0
