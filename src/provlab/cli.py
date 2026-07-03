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

from .llm import (
    AnthropicProseChannel,
    MockProseChannel,
    OpenAIProseChannel,
    ProseChannel,
)
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
    openai_model: str
    #: penalty grid for `prov-lab sweep` (defaults to the single configured penalty)
    penalty_grid: list[float]


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
        openai_model=str(prose.get("openai_model", "gpt-5-mini")),
        penalty_grid=[
            float(p)
            for p in raw.get("penalty_grid", [raw["reconstruction_penalty"]])
        ],
    )


def _make_channel(spec: ExperimentSpec, llm: str, seed: int) -> ProseChannel:
    if llm == "anthropic":
        return AnthropicProseChannel(model=spec.anthropic_model)
    if llm == "openai":
        return OpenAIProseChannel(model=spec.openai_model)
    return MockProseChannel(
        rng=np.random.default_rng(seed + PROSE_RNG_OFFSET),
        sigma=spec.prose_sigma,
        taint_recall=spec.prose_taint_recall,
        taint_precision=spec.prose_taint_precision,
        parse_failure_rate=spec.prose_parse_failure_rate,
    )


def cmd_run(args: argparse.Namespace) -> None:
    if args.trace is not None:
        _run_trace(args)
        return
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
        "steps": spec.steps,
        "reconstruction_penalty": spec.reconstruction_penalty,
        "recon_gate_thresholds": {
            p.name: threshold
            for p in policies
            if (threshold := p.recon_death_threshold()) is not None
        },
        "death_spiral_cadence": spec.death_spiral_cadence,
        "wall_seconds": round(time.monotonic() - t0, 2),
        "decision_log_sha256_last_run": result.decision_log_sha256,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"done: {total_runs} runs in {meta['wall_seconds']}s → {out_dir}/")


def _run_trace(args: argparse.Namespace) -> None:
    """`prov-lab run --trace path.jsonl`: replay a real agent trace through
    all four arms at every configured cadence. The oracle arm is the
    full-provenance replay of the same trace."""
    from .trace import (
        DEFAULT_TAINT_RULES,
        GenericJsonlAdapter,
        load_taint_rules,
        trace_to_events,
    )

    spec = load_spec(Path(args.config))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    policies = default_policies(allowlist_window=spec.allowlist_window)
    rules = (
        load_taint_rules(Path(args.taint_rules))
        if args.taint_rules is not None
        else list(DEFAULT_TAINT_RULES)
    )
    adapter = GenericJsonlAdapter(Path(args.trace))
    events, coverage = trace_to_events(
        adapter.records(), rules, decision_every=spec.decision_every
    )

    gate_rows: list[dict[str, Any]] = []
    drift_rows: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    prose_rows: list[dict[str, Any]] = []
    t0 = time.monotonic()
    sha = ""
    for cadence in spec.compaction_cadences:
        key = RunKey("main", cadence, "trace", 0)
        config = ReplayConfig(
            seed=0,
            steps=coverage.n_mapped,
            decision_every=spec.decision_every,
            compaction_cadence=cadence,
            keep_hops=spec.keep_hops,
            reconstruction_penalty=spec.reconstruction_penalty,
            profile=next(iter(spec.profiles.values())),  # unused in trace mode
            rehydrate=args.rehydrate,
            hop_log_path=out_dir / "hoplogs" / f"trace_c{cadence}.jsonl",
        )
        result = run_replay(
            config, policies, _make_channel(spec, args.llm, 0), events=list(events)
        )
        sha = result.decision_log_sha256
        gate_rows.extend(aggregate_gate_metrics(key, result.records))
        drift_rows.extend(aggregate_drift(key, result.records))
        curve_rows.extend(recon_curve_rows(key, result.recon_curve))
        prose_rows.append(prose_stats_row(key, result.prose_stats))

    pd.DataFrame(gate_rows).to_csv(out_dir / "gate_metrics.csv", index=False)
    pd.DataFrame(drift_rows).to_csv(out_dir / "drift.csv", index=False)
    pd.DataFrame(curve_rows).to_csv(out_dir / "recon_curve.csv", index=False)
    pd.DataFrame(prose_rows).to_csv(out_dir / "prose_channel.csv", index=False)
    # no synthetic death-spiral run in trace mode; the report tolerates this
    pd.DataFrame(
        columns=["run_type", "cadence", "profile", "seed", "step", "arm",
                 "policy", "proceed", "oracle_proceed"]
    ).to_csv(out_dir / "death_spiral_decisions.csv", index=False)
    (out_dir / "trace_coverage.json").write_text(
        json.dumps(coverage.as_json_obj(), indent=2)
    )
    meta = {
        "config": str(args.config),
        "trace": str(args.trace),
        "taint_rules": str(args.taint_rules) if args.taint_rules else "(built-in defaults)",
        "llm": args.llm,
        "rehydrate": args.rehydrate,
        "steps": coverage.n_mapped,
        "reconstruction_penalty": spec.reconstruction_penalty,
        "recon_gate_thresholds": {
            p.name: threshold
            for p in policies
            if (threshold := p.recon_death_threshold()) is not None
        },
        "wall_seconds": round(time.monotonic() - t0, 2),
        "decision_log_sha256_last_run": sha,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))
    print(
        f"done: trace of {coverage.n_mapped} mapped steps "
        f"({sum(coverage.skipped.values())} skipped) × "
        f"{len(spec.compaction_cadences)} cadences in {meta['wall_seconds']}s "
        f"→ {out_dir}/"
    )


def cmd_sweep(args: argparse.Namespace) -> None:
    """Cadence × penalty sweep (mock only): the data behind the crossover
    analytics in the report."""
    spec = load_spec(Path(args.config))
    if args.seeds is not None:
        spec.seeds = args.seeds
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    policies = default_policies(allowlist_window=spec.allowlist_window)

    rows: list[dict[str, Any]] = []
    cells = [
        (penalty, cadence, profile_name)
        for penalty in spec.penalty_grid
        for cadence in spec.compaction_cadences
        for profile_name in spec.profiles
    ]
    total = len(cells) * spec.seeds
    done = 0
    t0 = time.monotonic()
    for penalty, cadence, profile_name in cells:
        for seed in range(spec.seeds):
            key = RunKey("sweep", cadence, profile_name, seed)
            config = ReplayConfig(
                seed=seed,
                steps=spec.steps,
                decision_every=spec.decision_every,
                compaction_cadence=cadence,
                keep_hops=spec.keep_hops,
                reconstruction_penalty=penalty,
                profile=spec.profiles[profile_name],
                rehydrate=False,  # crossover only needs blind-mode flips
                hop_log_path=None,
            )
            result = run_replay(config, policies, _make_channel(spec, "mock", seed))
            for row in aggregate_gate_metrics(key, result.records):
                row["penalty"] = penalty
                rows.append(row)
            done += 1
            if done % 100 == 0:
                print(
                    f"[{time.monotonic() - t0:6.1f}s] {done}/{total} sweep runs",
                    flush=True,
                )

    pd.DataFrame(rows).to_csv(out_dir / "sweep_metrics.csv", index=False)
    meta = {
        "config": str(args.config),
        "steps": spec.steps,
        "penalty_grid": spec.penalty_grid,
        "cadences": spec.compaction_cadences,
        "seeds": spec.seeds,
        "wall_seconds": round(time.monotonic() - t0, 2),
    }
    (out_dir / "sweep_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"done: {total} sweep runs in {meta['wall_seconds']}s → {out_dir}/")


def cmd_audit(args: argparse.Namespace) -> None:
    """Static compaction/gate mismatch check — no simulation, no traces."""
    from .audit import AuditSpecError, load_audit_spec, render_findings, run_audit

    try:
        compaction, gate_specs = load_audit_spec(Path(args.spec))
    except AuditSpecError as err:
        raise SystemExit(f"audit spec error: {err}") from err
    findings = run_audit(compaction, gate_specs)
    rendered = render_findings(compaction, findings)
    print(rendered)
    if args.md is not None:
        Path(args.md).write_text(rendered + "\n")
        print(f"\nwrote {args.md}")


def cmd_report(args: argparse.Namespace) -> None:
    report_path = Path(__file__).resolve().parents[2] / "analysis" / "report.py"
    if not report_path.exists():
        report_path = Path("analysis/report.py")
    spec = importlib.util.spec_from_file_location("provlab_report", report_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load report module from {report_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_report(
        Path(args.out),
        llm_dir=Path(args.llm_out),
        sweep_dir=Path(args.sweep_out),
    )


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
        "--llm", dest="llm", choices=["anthropic", "openai"],
        help="real LLM prose channel (needs ANTHROPIC_API_KEY / OPENAI_API_KEY)",
    )
    p_run.set_defaults(llm="mock")
    p_run.add_argument(
        "--rehydrate", dest="rehydrate", action="store_true", default=True,
        help="evaluate lineage gates in rehydrate mode too (default on)",
    )
    p_run.add_argument("--no-rehydrate", dest="rehydrate", action="store_false")
    p_run.add_argument("--seeds", type=int, default=None)
    p_run.add_argument("--out", default="results")
    p_run.add_argument(
        "--trace", default=None,
        help="replay a real agent trace (generic JSONL schema, see provlab.trace) "
        "instead of the synthetic generator",
    )
    p_run.add_argument(
        "--taint-rules", default=None,
        help="YAML taint-derivation rules for --trace (defaults to the built-in rules)",
    )
    p_run.set_defaults(func=cmd_run)

    p_sweep = sub.add_parser(
        "sweep", help="cadence × penalty sweep (mock) for the crossover analytics"
    )
    p_sweep.add_argument("--config", default="experiments/config-sweep.yaml")
    p_sweep.add_argument("--seeds", type=int, default=None)
    p_sweep.add_argument("--out", default="results-sweep")
    p_sweep.set_defaults(func=cmd_sweep)

    p_audit = sub.add_parser(
        "audit",
        help="static check: which gate reads does your compaction starve?",
    )
    p_audit.add_argument("spec", help="YAML with compaction: and gates: sections")
    p_audit.add_argument("--md", default=None, help="also write the table to a file")
    p_audit.set_defaults(func=cmd_audit)

    p_report = sub.add_parser("report", help="build results/summary.md + figures")
    p_report.add_argument("--out", default="results")
    p_report.add_argument(
        "--llm-out", default="results-llm",
        help="real-LLM results dir for the matched-slice comparison "
        "(falls back to docs/data/llm_gate_metrics.csv)",
    )
    p_report.add_argument(
        "--sweep-out", default="results-sweep",
        help="sweep results dir for the crossover-vs-penalty analytics",
    )
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
