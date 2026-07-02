from __future__ import annotations

import pytest

from provlab.axes import (
    AXES,
    BASE_AXES,
    CAPABILITY,
    FRESHNESS,
    RECONSTRUCTION,
    ProvenanceVector,
    merge,
)


def test_canonical_axis_names() -> None:
    assert AXES == (
        "freshness",
        "capability",
        "tool_integrity",
        "verification",
        "reconstruction",
    )
    assert RECONSTRUCTION not in BASE_AXES


def test_pristine_vector() -> None:
    v = ProvenanceVector()
    assert all(v.scores[a] == 1.0 for a in AXES)


def test_min_merge_semantics() -> None:
    a = ProvenanceVector()
    a.degrade(FRESHNESS, 0.5)
    b = ProvenanceVector()
    b.degrade(CAPABILITY, 0.3)
    merged = merge([a, b])
    assert merged.scores[FRESHNESS] == 0.5
    assert merged.scores[CAPABILITY] == 0.3
    assert merged.scores[RECONSTRUCTION] == 1.0
    # merge is element-wise min, not average
    assert merged.scores[FRESHNESS] == min(a.scores[FRESHNESS], b.scores[FRESHNESS])


def test_merge_is_idempotent_and_commutative() -> None:
    a = ProvenanceVector()
    a.degrade(FRESHNESS, 0.7)
    b = ProvenanceVector()
    b.degrade(FRESHNESS, 0.4)
    assert merge([a, b]).scores == merge([b, a]).scores
    assert merge([a, a]).scores == a.scores


def test_degrade_clips_to_unit_interval() -> None:
    v = ProvenanceVector()
    for _ in range(100):
        v.degrade(FRESHNESS, 0.1)
    assert 0.0 <= v.scores[FRESHNESS] <= 1.0


def test_floor_is_running_min() -> None:
    v = ProvenanceVector()
    v.floor(RECONSTRUCTION, 0.8)
    v.floor(RECONSTRUCTION, 0.9)  # raising is impossible under min semantics
    assert v.scores[RECONSTRUCTION] == 0.8


def test_merge_empty_raises() -> None:
    with pytest.raises(ValueError):
        merge([])
