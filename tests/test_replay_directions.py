"""End-to-end direction-of-error checks over full replays (H3-style) and the
rehydration three-way comparison."""

from __future__ import annotations

from provlab.metrics import RunKey, aggregate_gate_metrics
from conftest import run_once


def _rows(seed: int = 0, steps: int = 500) -> list[dict[str, object]]:
    result = run_once(seed=seed, steps=steps, cadence=10)
    key = RunKey("main", 10, "med", seed)
    return aggregate_gate_metrics(key, result.records)


def test_error_directions_by_gate_style() -> None:
    rows = [
        r
        for seed in range(4)
        for r in _rows(seed=seed)
        if r["arm"] == "structural_min" and r["mode"] == "blind"
    ]
    fp_block = sum(int(str(r["n_false_proceed"])) for r in rows if r["gate_class"] == "lineage_blocklist")
    fs_block = sum(int(str(r["n_false_stop"])) for r in rows if r["gate_class"] == "lineage_blocklist")
    fp_allow = sum(int(str(r["n_false_proceed"])) for r in rows if r["gate_class"] == "lineage_allowlist")
    fs_allow = sum(int(str(r["n_false_stop"])) for r in rows if r["gate_class"] == "lineage_allowlist")
    # blocklist / default-allow → forgetting taints produces false-PROCEEDS
    assert fp_block > fs_block
    assert fp_block > 0
    # allowlist / default-deny → folding the proof produces false-STOPS
    assert fs_allow > fp_allow
    assert fs_allow > 0
    # blocklist can never false-stop: it only ever sees a subset of true taints
    assert fs_block == 0


def test_rehydrate_beats_blind_on_lineage_gates() -> None:
    rows = [
        r
        for seed in range(4)
        for r in _rows(seed=seed)
        if r["arm"] == "structural_min"
        and str(r["gate_class"]).startswith("lineage_")
    ]

    def flip_rate(mode: str) -> float:
        sel = [r for r in rows if r["mode"] == mode]
        n = sum(int(str(r["n"])) for r in sel)
        agree = sum(int(str(r["n_agree"])) for r in sel)
        return 1.0 - agree / n

    assert flip_rate("rehydrate") < flip_rate("blind")
    # rehydration pays in lookups
    lookups = sum(int(str(r["lookups"])) for r in rows if r["mode"] == "rehydrate")
    assert lookups > 0


def test_prose_flips_dominate_structural() -> None:
    rows = [r for seed in range(4) for r in _rows(seed=seed) if r["mode"] == "blind"]

    def flip_rate(arm: str) -> float:
        sel = [r for r in rows if r["arm"] == arm]
        n = sum(int(str(r["n"])) for r in sel)
        agree = sum(int(str(r["n_agree"])) for r in sel)
        return 1.0 - agree / n

    assert flip_rate("prose") > flip_rate("structural_min")
    assert flip_rate("prose") > flip_rate("structural_perhop")
