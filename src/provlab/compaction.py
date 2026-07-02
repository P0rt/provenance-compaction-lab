"""The four storage arms.

All arms observe the *same* trajectory (same seed, same events). They differ
only in how provenance is stored between decision points:

* ``ground_truth``      — full vector + full lineage, never compacted. The oracle.
* ``structural_min``    — the Part 4 scheme; ``reconstruction`` folded into the
                          vector with min (the variant Boyko says must decay to zero).
* ``structural_perhop`` — identical, except ``reconstruction`` is tracked
                          structurally and passed to gates as data.
* ``prose``             — the naive baseline: LLM summarize→extract replaces the
                          vector and taint set every C steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .axes import RECONSTRUCTION, ProvenanceVector, merge
from .lineage import FoldedPrefix, Hop, Lineage, merge_lineages
from .llm import ProseChannel


@dataclass
class ReconstructionRecord:
    """structural_perhop's reconstruction state, passed to gates as
    structured data instead of being folded into the vector.

    Mirrors Part 4's split of the reconstruction axis: *fidelity* is the
    worst single compaction (running-min over per-hop quality) while the
    boundary count accumulates separately — so fidelity does not decay just
    because many boundaries were crossed.
    """

    n_compactions: int = 0
    worst_single_penalty: float = 0.0
    penalties: list[float] = field(default_factory=list)

    def add(self, penalty: float) -> None:
        self.n_compactions += 1
        self.penalties.append(penalty)
        self.worst_single_penalty = max(self.worst_single_penalty, penalty)

    def fidelity(self) -> float:
        """Scalar a gate can derive: 1 - worst single compaction penalty."""
        return 1.0 - self.worst_single_penalty


@dataclass
class ValueState:
    vector: ProvenanceVector
    tainted_by: set[str]
    lineage: Lineage


@dataclass
class GateView:
    """What a gate sees for one value under one arm at a decision point."""

    arm: str
    #: raw vector scores, all five axes
    scores: dict[str, float]
    #: structural_perhop only: raw reconstruction history as structured data
    recon_record: ReconstructionRecord | None
    tainted_by: frozenset[str]
    visible_hops: tuple[Hop, ...]
    folded: FoldedPrefix | None
    #: True when part of this value's history is no longer inspectable
    history_truncated: bool

    def reconstruction_scalar(self) -> float:
        """The scalar a gate consults for the reconstruction axis. For
        structural_perhop this is derived from the structured record
        (fidelity = worst single compaction); everyone else reads the vector."""
        if self.recon_record is not None:
            return self.recon_record.fidelity()
        return self.scores[RECONSTRUCTION]

    def effective_scores(self) -> dict[str, float]:
        scores = dict(self.scores)
        scores[RECONSTRUCTION] = self.reconstruction_scalar()
        return scores


class Arm:
    """Full-fidelity provenance tracking. This *is* the ``ground_truth`` arm;
    the other arms subclass it and override only the storage boundary."""

    name = "ground_truth"

    def __init__(self) -> None:
        self.values: dict[int, ValueState] = {}

    # -- trajectory events -------------------------------------------------

    def on_new_value(self, value_id: int, hop: Hop) -> None:
        vector = ProvenanceVector()
        vector.floor(RECONSTRUCTION, self._initial_reconstruction())
        state = ValueState(vector=vector, tainted_by=set(), lineage=Lineage())
        self.values[value_id] = state
        self._apply_hop(state, hop)

    def on_hop(self, value_id: int, hop: Hop) -> None:
        self._apply_hop(self.values[value_id], hop)

    def on_merge(self, value_id: int, input_ids: tuple[int, ...], hop: Hop) -> None:
        inputs = [self.values[i] for i in input_ids]
        state = ValueState(
            vector=merge(s.vector for s in inputs),
            tainted_by=set().union(*(s.tainted_by for s in inputs)),
            lineage=merge_lineages(s.lineage for s in inputs),
        )
        self.values[value_id] = state
        self._apply_hop(state, hop)

    def retire(self, value_id: int) -> None:
        self.values.pop(value_id, None)

    def _apply_hop(self, state: ValueState, hop: Hop) -> None:
        for axis, factor in hop.axis_deltas.items():
            state.vector.degrade(axis, factor)
        state.tainted_by |= set(hop.taints_added)
        state.lineage.hops.append(hop)

    # -- storage boundary --------------------------------------------------

    def _initial_reconstruction(self) -> float:
        return 1.0

    def compact(self, step: int) -> None:
        """ground_truth never compacts: reconstruction stays 1.0 forever."""
        return None

    # -- gate interface ----------------------------------------------------

    def _recon_record(self) -> ReconstructionRecord | None:
        return None

    def view(self, value_id: int) -> GateView:
        state = self.values[value_id]
        return GateView(
            arm=self.name,
            scores=dict(state.vector.scores),
            recon_record=self._recon_record(),
            tainted_by=frozenset(state.tainted_by),
            visible_hops=tuple(state.lineage.hops),
            folded=state.lineage.folded,
            history_truncated=state.lineage.truncated,
        )


def truncate_lineage(state: ValueState, keep_hops: int) -> None:
    """Truncate to the last K hops; fold the prefix into aggregate counts and
    drop the folded taint ids from ``tainted_by`` (only the count survives)."""
    hops = state.lineage.hops
    if len(hops) <= keep_hops:
        return
    folded_now = hops[: len(hops) - keep_hops]
    kept = hops[len(hops) - keep_hops :]
    prefix = state.lineage.folded or FoldedPrefix()
    for hop in folded_now:
        prefix.absorb(hop)
    state.lineage.hops = list(kept)
    state.lineage.folded = prefix
    # invariant for structural arms: tainted_by == union of taints_added over
    # the surviving (visible) hops — folded taint ids are gone.
    state.tainted_by = set().union(*(set(h.taints_added) for h in kept)) if kept else set()


class StructuralMinArm(Arm):
    """The Part 4 scheme. Every C steps:

    * scores: keep the running min per axis — identical to ground truth for
      the four base axes *by construction* (compaction never touches them);
    * lineage: truncate to the last K hops, fold the prefix (taint ids dropped);
    * ``reconstruction``: multiply by (1 - penalty) per compaction and fold
      into the vector with min, like any other axis.

    Reconstruction is a property of the storage boundary, not of any single
    value: anything persisted in a store that has been compacted n times
    carries the store's reconstruction, so new values are stamped with the
    store-level factor at creation.
    """

    name = "structural_min"

    def __init__(self, keep_hops: int, penalty: float) -> None:
        super().__init__()
        self.keep_hops = keep_hops
        self.penalty = penalty
        self.store_reconstruction = 1.0
        self.n_compactions = 0

    def _initial_reconstruction(self) -> float:
        return self.store_reconstruction

    def compact(self, step: int) -> None:
        self.n_compactions += 1
        self.store_reconstruction *= 1.0 - self.penalty
        for value_id in sorted(self.values):
            state = self.values[value_id]
            state.vector.floor(RECONSTRUCTION, self.store_reconstruction)
            truncate_lineage(state, self.keep_hops)


class StructuralPerhopArm(Arm):
    """Identical to ``structural_min`` except ``reconstruction`` is NOT folded
    into the min: it is tracked as (n_compactions, worst_single_penalty,
    penalties) and passed to gates as structured data."""

    name = "structural_perhop"

    def __init__(self, keep_hops: int, penalty: float) -> None:
        super().__init__()
        self.keep_hops = keep_hops
        self.penalty = penalty
        self.record = ReconstructionRecord()

    def compact(self, step: int) -> None:
        self.record.add(self.penalty)
        for value_id in sorted(self.values):
            truncate_lineage(self.values[value_id], self.keep_hops)

    def _recon_record(self) -> ReconstructionRecord | None:
        return self.record


@dataclass
class ProseStats:
    n_extractions: int = 0
    n_parse_failures: int = 0
    n_true_taints: int = 0
    n_kept_taints: int = 0
    n_fabricated_taints: int = 0

    def realized_recall(self) -> float:
        return self.n_kept_taints / self.n_true_taints if self.n_true_taints else 1.0

    def realized_precision(self) -> float:
        reported = self.n_kept_taints + self.n_fabricated_taints
        return self.n_kept_taints / reported if reported else 1.0


class ProseArm(Arm):
    """The naive baseline (the strawman; honest but naive). Every C steps an
    LLM summarizes the window *including its provenance information* into
    ≤150 words of prose; a second call extracts axis scores + a taint list.
    The extraction REPLACES the arm's vector and taint set; lineage becomes
    just the prose blob."""

    name = "prose"

    def __init__(self, channel: ProseChannel) -> None:
        super().__init__()
        self.channel = channel
        self.stats = ProseStats()

    def compact(self, step: int) -> None:
        for value_id in sorted(self.values):
            state = self.values[value_id]
            out = self.channel.compress(
                scores=dict(state.vector.scores),
                taints=frozenset(state.tainted_by),
                step=step,
            )
            self.stats.n_extractions += 1
            self.stats.n_parse_failures += int(out.parse_failed)
            self.stats.n_true_taints += out.n_true_taints
            self.stats.n_kept_taints += out.n_kept
            self.stats.n_fabricated_taints += out.n_fabricated
            state.vector = ProvenanceVector(scores=dict(out.scores))
            state.tainted_by = set(out.taints)
            state.lineage = Lineage(hops=[], folded=None, prose_blob=out.blob)


def make_arms(
    keep_hops: int, penalty: float, channel: ProseChannel
) -> tuple[Arm, StructuralMinArm, StructuralPerhopArm, ProseArm]:
    return (
        Arm(),
        StructuralMinArm(keep_hops, penalty),
        StructuralPerhopArm(keep_hops, penalty),
        ProseArm(channel),
    )
