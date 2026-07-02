"""Aggregate decision records into blog-ready metrics.

Per arm vs ground_truth, per gate class and per gate:
* agreement_rate
* false_proceed_rate — arm proceeds, oracle blocks (the dangerous direction)
* false_stop_rate    — arm blocks, oracle proceeds (the expensive direction)
* per-axis score drift (MAE vs oracle at decision points)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .axes import AXES
from .compaction import ProseStats
from .replay import DecisionRecord, ReconPoint


@dataclass(frozen=True)
class RunKey:
    run_type: str  # "main" | "death_spiral"
    cadence: int
    profile: str
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_type": self.run_type,
            "cadence": self.cadence,
            "profile": self.profile,
            "seed": self.seed,
        }


GROUND_TRUTH = "ground_truth"


def _oracle_index(records: list[DecisionRecord]) -> dict[tuple[int, str], bool]:
    return {
        (r.step, r.policy): r.proceed
        for r in records
        if r.arm == GROUND_TRUTH and r.mode == "blind"
    }


def aggregate_gate_metrics(
    key: RunKey, records: list[DecisionRecord]
) -> list[dict[str, Any]]:
    """One row per (arm, policy, mode): counts of agreement / false-proceed /
    false-stop vs the oracle, plus rehydration cost counters."""
    oracle = _oracle_index(records)
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in records:
        if r.arm == GROUND_TRUTH:
            continue
        bucket_key = (r.arm, r.policy, r.mode)
        row = buckets.get(bucket_key)
        if row is None:
            row = {
                **key.as_dict(),
                "arm": r.arm,
                "policy": r.policy,
                "gate_class": r.gate_class,
                "irreversible": r.irreversible,
                "mode": r.mode,
                "n": 0,
                "n_agree": 0,
                "n_false_proceed": 0,
                "n_false_stop": 0,
                "n_proceed": 0,
                "n_oracle_proceed": 0,
                "lookups": 0,
                "bytes_read": 0,
            }
            buckets[bucket_key] = row
        oracle_proceed = oracle[(r.step, r.policy)]
        row["n"] += 1
        row["n_agree"] += int(r.proceed == oracle_proceed)
        row["n_false_proceed"] += int(r.proceed and not oracle_proceed)
        row["n_false_stop"] += int(not r.proceed and oracle_proceed)
        row["n_proceed"] += int(r.proceed)
        row["n_oracle_proceed"] += int(oracle_proceed)
        row["lookups"] += r.lookups
        row["bytes_read"] += r.bytes_read
    return [buckets[k] for k in sorted(buckets)]


def aggregate_drift(key: RunKey, records: list[DecisionRecord]) -> list[dict[str, Any]]:
    """Per-axis mean absolute error of arm scores vs oracle at decision points."""
    oracle_scores: dict[int, dict[str, float]] = {}
    arm_scores: dict[tuple[str, int], dict[str, float]] = {}
    for r in records:
        if r.mode != "blind":
            continue
        if r.arm == GROUND_TRUTH:
            oracle_scores[r.step] = r.scores
        else:
            arm_scores[(r.arm, r.step)] = r.scores
    sums: dict[tuple[str, str], list[float]] = {}
    for (arm, step), scores in arm_scores.items():
        oracle = oracle_scores[step]
        for axis in AXES:
            sums.setdefault((arm, axis), []).append(abs(scores[axis] - oracle[axis]))
    rows: list[dict[str, Any]] = []
    for (arm, axis) in sorted(sums):
        diffs = sums[(arm, axis)]
        rows.append(
            {
                **key.as_dict(),
                "arm": arm,
                "axis": axis,
                "mae": sum(diffs) / len(diffs),
                "n": len(diffs),
            }
        )
    return rows


def recon_curve_rows(key: RunKey, curve: list[ReconPoint]) -> list[dict[str, Any]]:
    return [
        {
            **key.as_dict(),
            "step": p.step,
            "n_compactions": p.n_compactions,
            "recon_min": p.recon_min,
            "fidelity_perhop": p.fidelity_perhop,
            "recon_prose": p.recon_prose,
        }
        for p in curve
    ]


def prose_stats_row(key: RunKey, stats: ProseStats) -> dict[str, Any]:
    return {
        **key.as_dict(),
        "n_extractions": stats.n_extractions,
        "n_parse_failures": stats.n_parse_failures,
        "parse_failure_rate": (
            stats.n_parse_failures / stats.n_extractions if stats.n_extractions else 0.0
        ),
        "n_true_taints": stats.n_true_taints,
        "n_kept_taints": stats.n_kept_taints,
        "n_fabricated_taints": stats.n_fabricated_taints,
        "realized_taint_recall": stats.realized_recall(),
        "realized_taint_precision": stats.realized_precision(),
    }


def death_spiral_rows(
    key: RunKey, records: list[DecisionRecord]
) -> list[dict[str, Any]]:
    """Per-decision rows for reconstruction-coupled gates (for finding the
    cycle count at which structural_min memory 'dies')."""
    oracle = _oracle_index(records)
    return [
        {
            **key.as_dict(),
            "step": r.step,
            "arm": r.arm,
            "policy": r.policy,
            "proceed": r.proceed,
            "oracle_proceed": oracle[(r.step, r.policy)],
        }
        for r in records
        if r.gate_class == "reconstruction" and r.mode == "blind" and r.arm != GROUND_TRUTH
    ]
