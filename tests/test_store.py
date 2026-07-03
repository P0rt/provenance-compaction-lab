from __future__ import annotations

from pathlib import Path

import pytest

from provlab.axes import ProvenanceVector
from provlab.compaction import ValueState, truncate_lineage
from provlab.lineage import Hop, Lineage
from provlab.store import ProvenanceStore


def hop(i: int, taints: frozenset[str] = frozenset()) -> Hop:
    return Hop(
        hop_id=f"h{i:06d}", step=i, op="TOOL_CALL",
        axis_deltas={}, taints_added=taints, parent_hop_ids=(),
    )


def compacted_state(store: ProvenanceStore) -> ValueState:
    """Build a value with 12 hops (taints at 2 and 10), append every hop to
    cold storage, compact to K=5, persist the compacted state."""
    hops = [
        hop(i, frozenset({f"taint:tool_flaky:{i}"}) if i in (2, 10) else frozenset())
        for i in range(12)
    ]
    for h in hops:
        store.append_hop(h)
    state = ValueState(
        vector=ProvenanceVector(),
        tainted_by={"taint:tool_flaky:2", "taint:tool_flaky:10"},
        lineage=Lineage(hops=list(hops)),
    )
    truncate_lineage(state, keep_hops=5)
    store.save_value(
        "v1", state.vector.scores, state.tainted_by,
        state.lineage.hops, state.lineage.folded,
    )
    return state


def test_blind_read_returns_compacted_state() -> None:
    with ProvenanceStore() as store:
        compacted_state(store)
        view = store.read_blind("v1")
        assert view.tainted_by == frozenset({"taint:tool_flaky:10"})  # 2 folded away
        assert len(view.hops) == 5
        assert view.history_truncated
        assert view.n_taints_folded == 1
        assert not view.untrusted and view.lookups == 0


def test_degraded_read_flags_untrusted() -> None:
    with ProvenanceStore() as store:
        compacted_state(store)
        assert store.read_degraded("v1").untrusted
        # a value with nothing folded is not degraded
        store.append_hop(hop(100))
        store.save_value("clean", {"freshness": 1.0}, set(), [hop(100)], None)
        assert not store.read_degraded("clean").untrusted


def test_rehydrated_read_recovers_full_history_with_cost() -> None:
    with ProvenanceStore() as store:
        compacted_state(store)
        view = store.read_rehydrated("v1")
        assert view.tainted_by == frozenset(
            {"taint:tool_flaky:2", "taint:tool_flaky:10"}
        )
        assert len(view.hops) == 12
        assert [h.step for h in view.hops] == list(range(12))  # ordering restored
        assert view.lookups == 7 and view.bytes_read > 0
        assert not view.history_truncated  # nothing folded from the gate's view


def test_append_only_log_rejects_conflicting_rewrite() -> None:
    with ProvenanceStore() as store:
        store.append_hop(hop(1))
        store.append_hop(hop(1))  # identical re-append is idempotent
        with pytest.raises(ValueError, match="append-only"):
            store.append_hop(hop(1, frozenset({"taint:tool_flaky:1"})))


def test_persistence_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "prov.sqlite"
    with ProvenanceStore(db) as store:
        compacted_state(store)
    with ProvenanceStore(db) as reopened:
        assert reopened.read_rehydrated("v1").tainted_by == frozenset(
            {"taint:tool_flaky:2", "taint:tool_flaky:10"}
        )


def test_missing_value_and_missing_hop_raise() -> None:
    with ProvenanceStore() as store:
        with pytest.raises(KeyError, match="no stored state"):
            store.read_blind("ghost")
        store.save_value("v", {"freshness": 1.0}, set(), [hop(1)], None)
        with pytest.raises(KeyError, match="missing from the append-only log"):
            store.read_blind("v")  # hop 1 was never appended to cold storage