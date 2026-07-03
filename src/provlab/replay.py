"""Replay: run all four arms over one trajectory and log every gate decision.

Order within a step: apply the step's op → compact (if step % C == 0) →
evaluate the decision point (if step % D == 0). Every decision (arm, gate,
proceed/block, scores seen, taints seen) is logged; the canonical-JSON hash
of the decision log is the determinism fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .axes import RECONSTRUCTION
from .compaction import (
    Arm,
    ProseArm,
    ProseStats,
    StructuralMinArm,
    StructuralPerhopArm,
    make_arms,
)
from .lineage import Hop, HopLog
from .llm import ProseChannel
from .policies import Policy
from .trajectory import (
    DecisionPoint,
    Event,
    MergeEvent,
    NewValue,
    OpEvent,
    Profile,
    Retire,
    generate_trajectory,
    unverified_fetch_deltas,
)


@dataclass(frozen=True)
class DecisionRecord:
    step: int
    arm: str
    policy: str
    gate_class: str
    irreversible: bool
    mode: str
    proceed: bool
    scores: dict[str, float]
    taints: tuple[str, ...]
    lookups: int
    bytes_read: int


@dataclass(frozen=True)
class ReconPoint:
    step: int
    n_compactions: int
    recon_min: float
    fidelity_perhop: float
    recon_prose: float


@dataclass
class ReplayResult:
    records: list[DecisionRecord]
    recon_curve: list[ReconPoint]
    prose_stats: ProseStats
    decision_log_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.decision_log_sha256:
            self.decision_log_sha256 = _sha256_of_records(self.records)


def _sha256_of_records(records: list[DecisionRecord]) -> str:
    payload = json.dumps(
        [asdict(r) for r in records], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ReplayConfig:
    seed: int
    steps: int
    decision_every: int
    compaction_cadence: int
    keep_hops: int
    reconstruction_penalty: float
    profile: Profile
    rehydrate: bool = True
    hop_log_path: Path | None = None


def _hop_for_event(event: Event, tips: dict[int, str]) -> Hop | None:
    """Build the (arm-independent) lineage hop for one trajectory event."""
    if isinstance(event, NewValue):
        step = event.step
        if event.axis is not None or event.taint is not None:
            # trace mode: rule-derived degradation attached to the fetch
            return Hop(
                hop_id=f"h{step:06d}",
                step=step,
                op="SOURCE_FETCH",
                axis_deltas={event.axis: event.factor} if event.axis else {},
                taints_added=frozenset({event.taint} if event.taint else set()),
                parent_hop_ids=(),
            )
        return Hop(
            hop_id=f"h{step:06d}",
            step=step,
            op="SOURCE_FETCH",
            axis_deltas=unverified_fetch_deltas(event.unverified),
            taints_added=frozenset(
                {f"taint:unverified_web:{step}"} if event.unverified else set()
            ),
            parent_hop_ids=(),
        )
    if isinstance(event, OpEvent):
        return Hop(
            hop_id=f"h{event.step:06d}",
            step=event.step,
            op=event.op,
            axis_deltas={event.axis: event.factor} if event.axis is not None else {},
            taints_added=frozenset({event.taint} if event.taint is not None else set()),
            parent_hop_ids=(tips[event.value_id],),
        )
    if isinstance(event, MergeEvent):
        return Hop(
            hop_id=f"h{event.step:06d}",
            step=event.step,
            op="MERGE",
            axis_deltas={},
            taints_added=frozenset(),
            parent_hop_ids=tuple(tips[i] for i in event.input_ids),
        )
    return None


def run_replay(
    config: ReplayConfig,
    policies: tuple[Policy, ...],
    channel: ProseChannel,
    events: list[Event] | None = None,
) -> ReplayResult:
    """Run all arms over one trajectory. ``events`` overrides the synthetic
    generator (trace mode — see provlab.trace); otherwise the trajectory is
    generated from the config's seed."""
    if events is None:
        events = generate_trajectory(
            seed=config.seed,
            steps=config.steps,
            decision_every=config.decision_every,
            profile=config.profile,
        )
    hop_log = HopLog(config.hop_log_path)
    ground_truth, structural_min, structural_perhop, prose = make_arms(
        config.keep_hops, config.reconstruction_penalty, channel
    )
    arms: tuple[Arm, ...] = (ground_truth, structural_min, structural_perhop, prose)
    structural_names = {structural_min.name, structural_perhop.name}

    records: list[DecisionRecord] = []
    recon_curve: list[ReconPoint] = []
    tips: dict[int, str] = {}  # value_id → last hop id (for parent links)
    last_compacted_at = 0

    def maybe_compact(step: int) -> None:
        nonlocal last_compacted_at
        if step > 0 and step % config.compaction_cadence == 0 and step != last_compacted_at:
            last_compacted_at = step
            for arm in arms:
                arm.compact(step)

    for event in events:
        hop = _hop_for_event(event, tips)
        if hop is not None:
            hop_log.append(hop)

        if isinstance(event, NewValue):
            assert hop is not None
            for arm in arms:
                arm.on_new_value(event.value_id, hop)
            tips[event.value_id] = hop.hop_id
        elif isinstance(event, OpEvent):
            assert hop is not None
            for arm in arms:
                arm.on_hop(event.value_id, hop)
            tips[event.value_id] = hop.hop_id
        elif isinstance(event, MergeEvent):
            assert hop is not None
            for arm in arms:
                arm.on_merge(event.value_id, event.input_ids, hop)
            tips[event.value_id] = hop.hop_id
        elif isinstance(event, Retire):
            for arm in arms:
                arm.retire(event.value_id)
            tips.pop(event.value_id, None)
        elif isinstance(event, DecisionPoint):
            maybe_compact(event.step)
            _evaluate_decision(
                event, arms, structural_names, policies, hop_log, config, records
            )
            recon_curve.append(
                ReconPoint(
                    step=event.step,
                    n_compactions=structural_min.n_compactions,
                    recon_min=structural_min.store_reconstruction,
                    fidelity_perhop=structural_perhop.record.fidelity(),
                    recon_prose=prose.values[event.value_id].vector.scores[
                        RECONSTRUCTION
                    ],
                )
            )
        if not isinstance(event, DecisionPoint):
            maybe_compact(event.step)

    hop_log.close()
    return ReplayResult(
        records=records, recon_curve=recon_curve, prose_stats=prose.stats
    )


def _evaluate_decision(
    event: DecisionPoint,
    arms: tuple[Arm, ...],
    structural_names: set[str],
    policies: tuple[Policy, ...],
    hop_log: HopLog,
    config: ReplayConfig,
    records: list[DecisionRecord],
) -> None:
    for arm in arms:
        view = arm.view(event.value_id)
        scores = {k: round(v, 6) for k, v in view.effective_scores().items()}
        taints = tuple(sorted(view.tainted_by))
        for policy in policies:
            modes = ["blind"]
            if policy.lineage_sensitive and arm.name in structural_names:
                modes.append("degrade")
                if config.rehydrate:
                    modes.append("rehydrate")
            for mode in modes:
                decision = policy.evaluate(view, mode=mode, hop_log=hop_log)
                records.append(
                    DecisionRecord(
                        step=event.step,
                        arm=arm.name,
                        policy=policy.name,
                        gate_class=policy.gate_class,
                        irreversible=policy.irreversible,
                        mode=mode,
                        proceed=decision.proceed,
                        scores=scores,
                        taints=taints,
                        lookups=decision.lookups,
                        bytes_read=decision.bytes_read,
                    )
                )
