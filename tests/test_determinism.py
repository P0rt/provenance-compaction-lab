from __future__ import annotations

from conftest import run_once


def test_same_seed_identical_decision_log_sha256() -> None:
    a = run_once(seed=3, steps=200)
    b = run_once(seed=3, steps=200)
    assert a.decision_log_sha256 == b.decision_log_sha256
    assert len(a.records) == len(b.records) > 0


def test_different_seed_different_decision_log() -> None:
    a = run_once(seed=3, steps=200)
    b = run_once(seed=4, steps=200)
    assert a.decision_log_sha256 != b.decision_log_sha256
