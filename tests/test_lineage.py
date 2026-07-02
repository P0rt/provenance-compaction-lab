from __future__ import annotations

from provlab.axes import CAPABILITY, ProvenanceVector
from provlab.compaction import StructuralMinArm, ValueState, truncate_lineage
from provlab.lineage import Hop, HopLog, Lineage


def hop(i: int, op: str = "TOOL_CALL", taints: frozenset[str] = frozenset()) -> Hop:
    return Hop(
        hop_id=f"h{i:06d}",
        step=i,
        op=op,
        axis_deltas={},
        taints_added=taints,
        parent_hop_ids=(),
    )


def make_state(n_hops: int, taint_at: dict[int, str]) -> ValueState:
    hops = [
        hop(i, taints=frozenset({taint_at[i]}) if i in taint_at else frozenset())
        for i in range(n_hops)
    ]
    return ValueState(
        vector=ProvenanceVector(),
        tainted_by=set(taint_at.values()),
        lineage=Lineage(hops=hops),
    )


def test_truncation_keeps_last_k_hops() -> None:
    state = make_state(12, {})
    truncate_lineage(state, keep_hops=5)
    assert len(state.lineage.hops) == 5
    assert [h.step for h in state.lineage.hops] == [7, 8, 9, 10, 11]
    assert state.lineage.folded is not None
    assert state.lineage.folded.n_hops_folded == 7


def test_folded_taint_ids_are_dropped_only_count_survives() -> None:
    state = make_state(12, {2: "taint:tool_flaky:2", 10: "taint:tool_flaky:10"})
    truncate_lineage(state, keep_hops=5)
    # the taint attached at hop 2 was folded away; only the count survives
    assert state.tainted_by == {"taint:tool_flaky:10"}
    assert state.lineage.folded is not None
    assert state.lineage.folded.n_taints_folded == 1
    # the folded hop ids point into cold storage
    assert "h000002" in state.lineage.folded.folded_hop_ids


def test_repeated_compaction_accumulates_fold_counts() -> None:
    state = make_state(12, {2: "taint:tool_flaky:2"})
    truncate_lineage(state, keep_hops=5)
    state.lineage.hops.extend(hop(i) for i in range(12, 18))
    truncate_lineage(state, keep_hops=5)
    assert state.lineage.folded is not None
    assert state.lineage.folded.n_hops_folded == 7 + 6
    assert len(state.lineage.hops) == 5


def test_op_counts_track_folded_ops() -> None:
    state = make_state(8, {})
    state.lineage.hops[0] = hop(0, op="SOURCE_FETCH")
    truncate_lineage(state, keep_hops=5)
    assert state.lineage.folded is not None
    assert state.lineage.folded.op_counts == {"SOURCE_FETCH": 1, "TOOL_CALL": 2}


def test_no_truncation_below_k() -> None:
    state = make_state(3, {1: "taint:stale_cache:1"})
    truncate_lineage(state, keep_hops=5)
    assert state.lineage.folded is None
    assert state.tainted_by == {"taint:stale_cache:1"}


def test_hop_log_roundtrip_and_bytes() -> None:
    log = HopLog()
    h = hop(7, taints=frozenset({"taint:fallback_model:7"}))
    log.append(h)
    fetched, bytes_read = log.fetch(["h000007"])
    assert fetched == [h]
    assert bytes_read > 0


def test_structural_min_reconstruction_untouched_by_base_degradation() -> None:
    arm = StructuralMinArm(keep_hops=5, penalty=0.02)
    arm.on_new_value(0, hop(0, op="SOURCE_FETCH"))
    arm.on_hop(
        0,
        Hop(
            hop_id="h000001",
            step=1,
            op="LLM_TRANSFORM",
            axis_deltas={CAPABILITY: 0.5},
            taints_added=frozenset(),
            parent_hop_ids=("h000000",),
        ),
    )
    view = arm.view(0)
    assert view.scores[CAPABILITY] == 0.5
    assert view.reconstruction_scalar() == 1.0  # no compaction yet
