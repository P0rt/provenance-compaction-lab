"""A1/A2: matched-slice comparison, analytic death cycle, crossover fit."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from provlab.policies import default_policies


def load_report_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "analysis" / "report.py"
    spec = importlib.util.spec_from_file_location("report_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recon_death_thresholds() -> None:
    thresholds = {
        p.name: t
        for p in default_policies()
        if (t := p.recon_death_threshold()) is not None
    }
    assert thresholds == {
        "archive_all_axes_floor": 0.5,
        "publish_discounted_thresholds": 0.55,
    }
    # score and lineage gates never consult reconstruction
    assert all(
        p.recon_death_threshold() is None
        for p in default_policies()
        if p.gate_class in ("score", "lineage_blocklist", "lineage_allowlist")
    )


def test_analytic_death_cycle_sanity_anchor() -> None:
    # θ = 0.5, p = 0.02 → 34.3, first whole cycle 35
    n_real = math.log(0.5) / math.log(1 - 0.02)
    assert n_real == pytest.approx(34.3, abs=0.05)
    assert math.floor(n_real) + 1 == 35


def _gate_row(
    cadence: int, seed: int, arm: str, gate_class: str, n: int, n_agree: int,
    n_fp: int = 0, n_fs: int = 0,
) -> dict[str, object]:
    return {
        "run_type": "main", "cadence": cadence, "profile": "med", "seed": seed,
        "arm": arm, "policy": f"g_{gate_class}", "gate_class": gate_class,
        "irreversible": False, "mode": "blind", "n": n, "n_agree": n_agree,
        "n_false_proceed": n_fp, "n_false_stop": n_fs, "n_proceed": 0,
        "n_oracle_proceed": 0, "lookups": 0, "bytes_read": 0,
    }


def test_matched_slice_uses_only_intersecting_cells(tmp_path: Path) -> None:
    report = load_report_module()
    # mock results cover seeds 0..2; llm results cover seeds 0..1 → intersection {0,1}
    mock = pd.DataFrame(
        [_gate_row(10, s, "prose", "score", 100, 90) for s in (0, 1, 2)]
    )
    llm_dir = tmp_path / "llm"
    llm_dir.mkdir()
    pd.DataFrame(
        [_gate_row(10, s, "prose", "score", 100, 50) for s in (0, 1)]
    ).to_csv(llm_dir / "gate_metrics.csv", index=False)
    lines = report._matched_slice_lines(mock, llm_dir)
    text = "\n".join(lines)
    assert "identical cells only" in text
    assert "seeds {0, 1}" in text  # seed 2 excluded — never silently mixed
    assert "| prose | score | 10.00% | 50.00% |" in text


def test_matched_slice_skips_cleanly_without_llm_results(tmp_path: Path) -> None:
    report = load_report_module()
    module_frozen = report.FROZEN_LLM_GATE_METRICS
    mock = pd.DataFrame([_gate_row(10, 0, "prose", "score", 100, 90)])
    # point the frozen fallback somewhere empty for this test
    report.FROZEN_LLM_GATE_METRICS = tmp_path / "missing.csv"
    try:
        lines = report._matched_slice_lines(mock, tmp_path / "absent")
    finally:
        report.FROZEN_LLM_GATE_METRICS = module_frozen
    assert any("Skipped" in line for line in lines)


def test_crossover_interpolation_and_fit(tmp_path: Path) -> None:
    report = load_report_module()
    # synthetic sweep: min flip = 10x prose below the crossing cadence, 0 above;
    # crossing placed at cadence ∝ 1/p so the fit recovers exponent ≈ −1
    rows: list[dict[str, object]] = []
    cadences = [5, 10, 20, 40, 80, 160]
    for penalty, cross_at in ((0.01, 10), (0.02, 20), (0.04, 40), (0.08, 80)):
        for cadence in cadences:
            min_flip = 40 if cadence < cross_at else 0
            for arm, flips in (("structural_min", min_flip), ("prose", 4)):
                row = _gate_row(cadence, 0, arm, "reconstruction", 100, 100 - flips)
                row["penalty"] = penalty
                rows.append(row)
    sweep_dir = tmp_path / "sweep"
    sweep_dir.mkdir()
    pd.DataFrame(rows).to_csv(sweep_dir / "sweep_metrics.csv", index=False)
    (sweep_dir / "sweep_meta.json").write_text(json.dumps({"steps": 500}))
    lines = report._crossover_lines(sweep_dir)
    text = "\n".join(lines)
    assert "Crossover vs reconstruction penalty" in text
    assert "p^-1.0" in text or "roughly constant" in text


def test_crossover_skips_without_sweep(tmp_path: Path) -> None:
    report = load_report_module()
    # neutralize the committed frozen fallback so "no sweep data" is testable
    frozen = report.FROZEN_SWEEP_METRICS
    report.FROZEN_SWEEP_METRICS = tmp_path / "missing.csv"
    try:
        assert report._crossover_lines(tmp_path / "nope") == []
        assert report._crossover_lines(None) == []
    finally:
        report.FROZEN_SWEEP_METRICS = frozen
