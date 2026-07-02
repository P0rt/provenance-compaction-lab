"""CLI entrypoints.

    uv run prov-lab run    --config experiments/config.yaml [--mock|--llm anthropic]
                           [--rehydrate/--no-rehydrate] [--seeds N]
    uv run prov-lab report
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .llm import AnthropicProseChannel, MockProseChannel, ProseChannel
from .metrics import (
    RunKey,
    aggregate_drift,
    aggregate_gate_metrics,
    death_spiral_rows,
    prose_stats_row,
    recon_curve_rows,
)
from .policies import default_policies
from .replay import ReplayConfig, run_replay
from .trajectory import Profile

#: offset so the prose channel's rng never collides with the trajectory rng
PROSE_RNG_OFFSET = 1_000_003


@dataclass
class ExperimentSpec:
    steps: int
    decision_every: int
    compaction_cadences: list[int]
    keep_hops: int
    allowlist_window: int
    reconstruction_penalty: float
    profiles: dict[str, Profile]
    seeds: int
    death_spiral_steps: int
    death_spiral_cadence: int
    prose_sigma: float
    prose_taint_recall: float
    prose_taint_precision: float
    prose_parse_failure_rate: float
    anthropic_model: str


def load_spec(path: Path) -> ExperimentSpec:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    prose = raw.get("prose", {})
    return ExperimentSpec(
        steps=int(raw["steps"]),
        decision_every=int(raw["decision_every"]),
        compaction_cadences=[int(c) for c in raw["compaction_cadence"]],
        keep_hops=int(raw["lineage_keep_hops"]),
        allowlist_window=int(raw["allowlist_window"]),
        reconstruction_penalty=float(raw["reconstruction_penalty"]),
        profiles={
            name: Profile(
                p_unverified=float(p["p_unverified"]),
                p_fallback=float(p["p_fallback"]),
                p_flaky=float(p["p_flaky"]),
                p_stale=float(p["p_stale"]),
            )
            for name, p in raw["degradation_profiles"].items()
        },
        seeds=int(raw["seeds"]),
        death_spiral_steps=int(raw["death_spiral_run"]["steps"]),
        death_spiral_cadence=int(raw["death_spiral_run"]["compaction_cadence"]),
        prose_sigma=float(prose.get("sigma", 0.08)),
        prose_taint_recall=float(prose.get("taint_recall", 0.6)),
        prose_taint_precision=float(prose.get("taint_precision", 0.9)),
        prose_parse_failure_rate=float(prose.get("parse_failure_rate", 0.0)),
        anthropic_model=str(prose.get("anthropic_model", "claude-haiku-4-5")),
    )


def _make_channel(spec: ExperimentSpec, llm: str, seed: int) -> ProseChannel:
    if llm == "anthropic":
        return AnthropicProseChannel(model=spec.anthropic_model)
    return MockProseChannel(
        rng=np.random.default_rng(seed + PROSE_RNG_OFFSET),
        sigma=spec.prose_sigma,
        taint_recall=spec.prose_taint_recall,
        taint_precision=spec.prose_taint_precision,
        parse_failure_rate=spec.prose_parse_failure_rate,
    )


def cmd_run(args: argparse.Namespace) -> None:
    spec = load_spec(Path(args.config))
    if args.seeds is not None:
        spec.seeds = args.seeds
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    policies = default_policies(allowlist_window=spec.allowlist_window)

    gate_rows: list[dict[str, Any]] = []
    drift_rows: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    prose_rows: list[dict[str, Any]] = []
    death_rows: list[dict[str, Any]] = []

    cells = [
        (cadence, profile_name)
        for cadence in spec.compaction_cadences
        for profile_name in spec.profiles
    ]
    total_runs = len(cells) * spec.seeds + 1
    done = 0
    t0 = time.monotonic()

    for cadence, profile_name in cells:
        for seed in range(spec.seeds):
            key = RunKey("main", cadence, profile_name, seed)
            config = ReplayConfig(
                seed=seed,
                steps=spec.steps,
                decision_every=spec.decision_every,
                compaction_cadence=cadence,
                keep_hops=spec.keep_hops,
                reconstruction_penalty=spec.reconstruction_penalty,
                profile=spec.profiles[profile_name],
                rehydrate=args.rehydrate,
                hop_log_path=out_dir
                / "hoplogs"
                / f"main_c{cadence}_{profile_name}_s{seed}.jsonl",
            )
            result = run_replay(config, policies, _make_channel(spec, args.llm, seed))
            gate_rows.extend(aggregate_gate_metrics(key, result.records))
            drift_rows.extend(aggregate_drift(key, result.records))
            curve_rows.extend(recon_curve_rows(key, result.recon_curve))
            prose_rows.append(prose_stats_row(key, result.prose_stats))
            done += 1
            if done % 20 == 0 or done == total_runs - 1:
                elapsed = time.monotonic() - t0
                print(f"[{elapsed:6.1f}s] {done}/{total_runs} runs", flush=True)

    # death-spiral run: long horizon, single seed, med profile
    key = RunKey("death_spiral", spec.death_spiral_cadence, "med", 0)
    config = ReplayConfig(
        seed=0,
        steps=spec.death_spiral_steps,
        decision_every=spec.decision_every,
        compaction_cadence=spec.death_spiral_cadence,
        keep_hops=spec.keep_hops,
        reconstruction_penalty=spec.reconstruction_penalty,
        profile=spec.profiles["med"],
        rehydrate=args.rehydrate,
        hop_log_path=out_dir / "hoplogs" / "death_spiral.jsonl",
    )
    result = run_replay(config, policies, _make_channel(spec, args.llm, 0))
    gate_rows.extend(aggregate_gate_metrics(key, result.records))
    curve_rows.extend(recon_curve_rows(key, result.recon_curve))
    prose_rows.append(prose_stats_row(key, result.prose_stats))
    death_rows.extend(death_spiral_rows(key, result.records))

    pd.DataFrame(gate_rows).to_csv(out_dir / "gate_metrics.csv", index=False)
    pd.DataFrame(drift_rows).to_csv(out_dir / "drift.csv", index=False)
    pd.DataFrame(curve_rows).to_csv(out_dir / "recon_curve.csv", index=False)
    pd.DataFrame(prose_rows).to_csv(out_dir / "prose_channel.csv", index=False)
    pd.DataFrame(death_rows).to_csv(out_dir / "death_spiral_decisions.csv", index=False)

    meta = {
        "config": str(args.config),
        "llm": args.llm,
        "rehydrate": args.rehydrate,
        "seeds": spec.seeds,
        "runs": total_runs,
        "wall_seconds": round(time.monotonic() - t0, 2),
        "decision_log_sha256_last_run": result.decision_log_sha256,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"done: {total_runs} runs in {meta['wall_seconds']}s → {out_dir}/")


def cmd_report(args: argparse.Namespace) -> None:
    report_path = Path(__file__).resolve().parents[2] / "analysis" / "report.py"
    if not report_path.exists():
        report_path = Path("analysis/report.py")
    spec = importlib.util.spec_from_file_location("provlab_report", report_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load report module from {report_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_report(Path(args.out))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prov-lab")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the experiment matrix")
    p_run.add_argument("--config", default="experiments/config.yaml")
    group = p_run.add_mutually_exclusive_group()
    group.add_argument(
        "--mock", dest="llm", action="store_const", const="mock",
        help="simulated noisy prose channel (default; no API key needed)",
    )
    group.add_argument(
        "--llm", dest="llm", choices=["anthropic"],
        help="real LLM prose channel (needs ANTHROPIC_API_KEY)",
    )
    p_run.set_defaults(llm="mock")
    p_run.add_argument(
        "--rehydrate", dest="rehydrate", action="store_true", default=True,
        help="evaluate lineage gates in rehydrate mode too (default on)",
    )
    p_run.add_argument("--no-rehydrate", dest="rehydrate", action="store_false")
    p_run.add_argument("--seeds", type=int, default=None)
    p_run.add_argument("--out", default="results")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="build results/summary.md + figures")
    p_report.add_argument("--out", default="results")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
