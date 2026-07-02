"""Both lineage gate styles, with hand-built views: the direction of error is
the design choice, and rehydration recovers the oracle decision."""

from __future__ import annotations

from provlab.axes import pristine_scores
from provlab.compaction import GateView, ReconstructionRecord
from provlab.lineage import FoldedPrefix, Hop, HopLog
from provlab.policies import AllowlistGate, BlocklistGate, DiscountedGate, MinFloorGate


def hop(i: int, taints: frozenset[str] = frozenset()) -> Hop:
    return Hop(
        hop_id=f"h{i:06d}",
        step=i,
        op="TOOL_CALL",
        axis_deltas={},
        taints_added=taints,
        parent_hop_ids=(),
    )


def make_view(
    *,
    tainted_by: frozenset[str] = frozenset(),
    visible_hops: tuple[Hop, ...] = (),
    folded: FoldedPrefix | None = None,
    scores: dict[str, float] | None = None,
    recon_record: ReconstructionRecord | None = None,
    arm: str = "structural_min",
) -> GateView:
    return GateView(
        arm=arm,
        scores=scores or pristine_scores(),
        recon_record=recon_record,
        tainted_by=tainted_by,
        visible_hops=visible_hops,
        folded=folded,
        history_truncated=folded is not None,
    )


BLOCK = BlocklistGate("g_block", ("taint:unverified_web:",), irreversible=True)
ALLOW = AllowlistGate("g_allow", 8, ("taint:fallback_model:",), irreversible=True)


def folded_setup() -> tuple[HopLog, FoldedPrefix]:
    """Cold storage holds a folded hop carrying an unverified-web taint."""
    log = HopLog()
    bad = hop(2, taints=frozenset({"taint:unverified_web:2"}))
    log.append(bad)
    prefix = FoldedPrefix()
    prefix.absorb(bad)
    return log, prefix


def test_blocklist_forgets_folded_taint_false_proceed() -> None:
    log, prefix = folded_setup()
    view = make_view(tainted_by=frozenset(), folded=prefix)
    # blind: the taint id was dropped at compaction → the gate proceeds
    assert BLOCK.evaluate(view, mode="blind").proceed is True
    # ...but the oracle (full taint set) would have blocked
    oracle_view = make_view(tainted_by=frozenset({"taint:unverified_web:2"}))
    assert BLOCK.evaluate(oracle_view, mode="blind").proceed is False


def test_blocklist_degrade_treats_missing_detail_as_untrusted() -> None:
    _, prefix = folded_setup()
    view = make_view(folded=prefix)
    assert BLOCK.evaluate(view, mode="degrade").proceed is False


def test_blocklist_rehydrate_recovers_oracle_decision_and_counts_cost() -> None:
    log, prefix = folded_setup()
    view = make_view(folded=prefix)
    decision = BLOCK.evaluate(view, mode="rehydrate", hop_log=log)
    assert decision.proceed is False  # matches the oracle again
    assert decision.lookups == 1
    assert decision.bytes_read > 0


def test_allowlist_blocks_when_window_folded_false_stop() -> None:
    log = HopLog()
    clean_hops = [hop(i) for i in range(10)]
    for h in clean_hops:
        log.append(h)
    prefix = FoldedPrefix()
    for h in clean_hops[:5]:
        prefix.absorb(h)
    # only 5 visible hops but W=8, and history is truncated → cannot prove
    view = make_view(visible_hops=tuple(clean_hops[5:]), folded=prefix)
    assert ALLOW.evaluate(view, mode="blind").proceed is False
    # the oracle sees all 10 hops (clean) and proceeds
    oracle_view = make_view(visible_hops=tuple(clean_hops), arm="ground_truth")
    assert ALLOW.evaluate(oracle_view, mode="blind").proceed is True
    # rehydration re-expands the window and recovers the proceed
    rehydrated = ALLOW.evaluate(view, mode="rehydrate", hop_log=log)
    assert rehydrated.proceed is True
    assert rehydrated.lookups == 5


def test_allowlist_short_but_complete_history_is_provable() -> None:
    view = make_view(visible_hops=(hop(0), hop(1)), folded=None)
    assert ALLOW.evaluate(view, mode="blind").proceed is True


def test_allowlist_checks_forbidden_taint_inside_window() -> None:
    hops = tuple(
        hop(i, taints=frozenset({"taint:fallback_model:5"}) if i == 5 else frozenset())
        for i in range(9)
    )
    view = make_view(visible_hops=hops)
    assert ALLOW.evaluate(view, mode="blind").proceed is False
    # the fallback hop slides out of the last-W window
    old = tuple(
        hop(i, taints=frozenset({"taint:fallback_model:0"}) if i == 0 else frozenset())
        for i in range(9)
    )
    assert ALLOW.evaluate(make_view(visible_hops=old), mode="blind").proceed is True


def test_reconstruction_gate_min_vs_perhop_divergence() -> None:
    gate = MinFloorGate("g_floor", floor=0.5, irreversible=False)
    # after 40 compactions at 0.02: min arm reads 0.98^40 ≈ 0.446 → block
    min_scores = pristine_scores()
    min_scores["reconstruction"] = 0.98**40
    assert gate.evaluate(make_view(scores=min_scores)).proceed is False
    # perhop derives fidelity = 1 - worst_single_penalty = 0.98 → proceed
    record = ReconstructionRecord()
    for _ in range(40):
        record.add(0.02)
    assert (
        gate.evaluate(
            make_view(recon_record=record, arm="structural_perhop")
        ).proceed
        is True
    )


def test_discounted_gate_uses_reconstruction_scalar() -> None:
    gate = DiscountedGate("g_disc", {"freshness": 0.55}, irreversible=False)
    scores = pristine_scores()
    scores["reconstruction"] = 0.5
    assert gate.evaluate(make_view(scores=scores)).proceed is False
    scores["reconstruction"] = 1.0
    assert gate.evaluate(make_view(scores=scores)).proceed is True
