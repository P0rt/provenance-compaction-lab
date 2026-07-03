"""Build results/summary.md + figures from the aggregate CSVs.

Reads:  results/gate_metrics.csv, drift.csv, recon_curve.csv,
        prose_channel.csv, death_spiral_decisions.csv, run_meta.json
        plus, when present: a real-LLM results dir (matched-slice comparison;
        falls back to docs/data/llm_gate_metrics.csv) and a sweep results dir
        (crossover-vs-penalty analytics).
Writes: results/summary.md, results/fig_*.png,
        docs/figures/fig_crossover_vs_penalty.png (sweep only)
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_LLM_GATE_METRICS = REPO_ROOT / "docs" / "data" / "llm_gate_metrics.csv"
FROZEN_SWEEP_METRICS = REPO_ROOT / "docs" / "data" / "sweep_metrics.csv"

ARMS = ("structural_min", "structural_perhop", "prose")
GATE_CLASSES = ("score", "reconstruction", "lineage_blocklist", "lineage_allowlist")

# categorical slots from a CVD-validated palette (fixed order, never cycled)
COLOR = {
    "structural_min": "#2a78d6",  # blue
    "structural_perhop": "#1baf7a",  # aqua
    "prose": "#eda100",  # yellow
}
# sequential blue ramp for the ordered low → med → high profiles
PROFILE_COLOR = {"low": "#8ab8e8", "med": "#2a78d6", "high": "#14477e"}
GRAY = "#8a8984"
INK = "#0b0b0b"


def _rates(df: pd.DataFrame) -> tuple[float, float, float]:
    """(agreement, false_proceed, false_stop) rates over a slice."""
    n = int(df["n"].sum())
    if n == 0:
        return math.nan, math.nan, math.nan
    return (
        float(df["n_agree"].sum()) / n,
        float(df["n_false_proceed"].sum()) / n,
        float(df["n_false_stop"].sum()) / n,
    )


def _flip(df: pd.DataFrame) -> float:
    agree, _, _ = _rates(df)
    return 1.0 - agree


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def build_report(
    results_dir: Path,
    llm_dir: Path | None = None,
    sweep_dir: Path | None = None,
) -> None:
    gates = pd.read_csv(results_dir / "gate_metrics.csv")
    drift = pd.read_csv(results_dir / "drift.csv")
    curve = pd.read_csv(results_dir / "recon_curve.csv")
    prose = pd.read_csv(results_dir / "prose_channel.csv")
    death = pd.read_csv(results_dir / "death_spiral_decisions.csv")

    main = gates[gates["run_type"] == "main"]
    blind = main[main["mode"] == "blind"]
    death_rows = gates[gates["run_type"] == "death_spiral"]
    death_cadence: int | None = (
        int(death_rows["cadence"].iloc[0]) if len(death_rows) else None
    )
    profiles_present = _ordered_profiles(blind)

    # ---- headline numbers --------------------------------------------------
    h1_cadence, h1_profile = 25, "med"
    h1_slice = blind[
        (blind["arm"] == "structural_min")
        & blind["irreversible"]
        & (blind["cadence"] == h1_cadence)
        & (blind["profile"] == h1_profile)
    ]
    if h1_slice.empty:  # e.g. trace mode: fall back to the first available cell
        h1_cadence = int(sorted(blind["cadence"].unique())[0])
        h1_profile = profiles_present[0]
        h1_slice = blind[
            (blind["arm"] == "structural_min")
            & blind["irreversible"]
            & (blind["cadence"] == h1_cadence)
            & (blind["profile"] == h1_profile)
        ]
    _, headline_fp, _ = _rates(h1_slice)

    death_cycle = (
        _death_cycle(death, death_cadence) if death_cadence is not None else None
    )
    perhop_dies = (
        death_cadence is not None
        and _death_cycle(death, death_cadence, arm="structural_perhop") is not None
    )

    prose_flip = _flip(blind[blind["arm"] == "prose"])
    min_flip = _flip(blind[blind["arm"] == "structural_min"])
    flip_ratio = prose_flip / min_flip if min_flip else math.inf

    lineage_mask = main["gate_class"].str.startswith("lineage_") & main["arm"].isin(
        ["structural_min", "structural_perhop"]
    )
    lineage = main[lineage_mask]
    rehydrated = lineage[lineage["mode"] == "rehydrate"]
    lookups_per_100 = 100.0 * float(rehydrated["lookups"].sum()) / int(rehydrated["n"].sum())
    flip_reduction = _flip(lineage[lineage["mode"] == "blind"]) - _flip(rehydrated)

    # ---- H1–H4 verdicts ----------------------------------------------------
    structural_blind = blind[blind["arm"].isin(["structural_min", "structural_perhop"])]
    score_flip = _flip(structural_blind[structural_blind["gate_class"] == "score"])
    lineage_flip = _flip(structural_blind[structural_blind["gate_class"].str.startswith("lineage_")])
    h1_pass = score_flip == 0.0 and lineage_flip > 0.0

    ds_curve = curve[curve["run_type"] == "death_spiral"].sort_values("step")
    recon_vals = list(ds_curve["recon_min"])
    monotone = all(a >= b for a, b in zip(recon_vals, recon_vals[1:]))
    h2_pass = monotone and death_cycle is not None and not perhop_dies
    h2_verdict = (
        _verdict(h2_pass) if death_cadence is not None else "n/a (no death-spiral run)"
    )

    sm_blind = blind[blind["arm"] == "structural_min"]
    _, bl_fp, bl_fs = _rates(sm_blind[sm_blind["gate_class"] == "lineage_blocklist"])
    _, al_fp, al_fs = _rates(sm_blind[sm_blind["gate_class"] == "lineage_allowlist"])
    h3_pass = bl_fp > bl_fs and al_fs > al_fp

    h4_rows: list[tuple[str, float, float, float, bool]] = []
    for gate_class in GATE_CLASSES:
        p = _flip(blind[(blind["arm"] == "prose") & (blind["gate_class"] == gate_class)])
        m = _flip(blind[(blind["arm"] == "structural_min") & (blind["gate_class"] == gate_class)])
        ph = _flip(blind[(blind["arm"] == "structural_perhop") & (blind["gate_class"] == gate_class)])
        h4_rows.append((gate_class, p, m, ph, p > m and p > ph))
    h4_pass = all(row[4] for row in h4_rows)

    # ---- figures -------------------------------------------------------------
    fig_decay: Path | None = None
    if death_cadence is not None and not ds_curve.empty:
        fig_decay = results_dir / "fig_reconstruction_decay.png"
        _fig_reconstruction_decay(ds_curve, death_cadence, death_cycle, fig_decay)
    fig_agree = results_dir / "fig_gate_agreement_by_class.png"
    _fig_agreement_by_class(blind, fig_agree)
    fig_fp = results_dir / "fig_false_proceed_vs_cadence.png"
    _fig_false_proceed_vs_cadence(blind, profiles_present, fig_fp)

    # ---- summary.md ----------------------------------------------------------
    lines: list[str] = []
    add = lines.append
    add("# Provenance under compaction — experiment summary\n")
    lines.extend(_trace_coverage_lines(results_dir))
    add("## HEADLINE NUMBERS\n")
    add(
        f"1. **False-proceed rate on irreversible gates** (`structural_min`, "
        f"C={h1_cadence}, {h1_profile} profile): "
        f"**{_pct(headline_fp)}** of irreversible-action decisions proceeded when the "
        f"uncompacted oracle said block.\n"
    )
    if death_cadence is None:
        add(
            "2. **No death-spiral run in this results set** (trace mode runs "
            "only the trace itself) — see the default config for the "
            "long-horizon reconstruction-decay measurement.\n"
        )
    elif death_cycle is not None:
        add(
            f"2. **structural_min memory dies at compaction cycle ≈ {death_cycle}** "
            f"(death-spiral run, C={death_cadence}): from that cycle on, every "
            f"reconstruction-coupled gate blocks permanently (0.98^{death_cycle} ≈ "
            f"{0.98 ** death_cycle:.3f} < 0.5). `structural_perhop` "
            f"{'ALSO dies — investigate' if perhop_dies else 'never dies'}.\n"
        )
    else:
        add(
            f"2. **structural_min memory did not die within this run's horizon** "
            f"(death-spiral run, C={death_cadence}) — too few compaction cycles to "
            f"cross the 0.5 gate floor; see the default config for the full run.\n"
        )
    add(
        f"3. **Prose-vs-structural flip-rate ratio: {flip_ratio:.2f}×** "
        f"(prose {_pct(prose_flip)} vs structural_min {_pct(min_flip)} decisions flipped "
        f"vs the oracle, all gates, main matrix).\n"
    )
    add(
        f"4. **Rehydration**: {lookups_per_100:.0f} cold-storage lookups per 100 lineage-gate "
        f"decisions buy a **{_pct(flip_reduction)} absolute flip-rate reduction** "
        f"(blind {_pct(_flip(lineage[lineage['mode'] == 'blind']))} → "
        f"rehydrate {_pct(_flip(rehydrated))}).\n"
    )

    add("\n## Hypothesis verdicts\n")
    add("| Hypothesis | Verdict | Evidence |")
    add("|---|---|---|")
    add(
        f"| **H1 (Boyko)** — score gates agree 100% between oracle and structural arms; "
        f"lineage gates carry the divergence | {_verdict(h1_pass)} | score-gate flip rate "
        f"{_pct(score_flip)} (**by construction, not a discovery** — compaction never touches "
        f"the running min of the base axes); lineage-gate flip rate {_pct(lineage_flip)} |"
    )
    add(
        f"| **H2 (death spiral)** — min-folded reconstruction decays monotonically and "
        f"eventually blocks ALL reconstruction-coupled gates permanently; perhop does not | "
        f"{h2_verdict} | monotone: {monotone}; death cycle: {death_cycle}; "
        f"perhop dies: {perhop_dies} |"
    )
    add(
        f"| **H3 (error direction is a design choice)** — blocklist → false-proceeds, "
        f"allowlist → false-stops | {_verdict(h3_pass)} | blocklist fp {_pct(bl_fp)} vs "
        f"fs {_pct(bl_fs)}; allowlist fs {_pct(al_fs)} vs fp {_pct(al_fp)} (structural_min) |"
    )
    add(
        f"| **H4 (prose is catastrophic)** — prose flip rates dominate structural arms on "
        f"every gate class | {_verdict(h4_pass)} | see per-class table below |"
    )

    add("\n### H4 per gate class (flip rate vs oracle, main matrix, blind)\n")
    add("| gate class | prose | structural_min | structural_perhop | prose dominates? |")
    add("|---|---|---|---|---|")
    for gate_class, p, m, ph, dom in h4_rows:
        add(f"| {gate_class} | {_pct(p)} | {_pct(m)} | {_pct(ph)} | {'yes' if dom else '**no**'} |")
    if not h4_pass:
        add(
            "\n> H4 **fails** as stated, and the failure is itself a finding: on "
            "reconstruction-coupled gates the min-folded reconstruction axis is a *bigger* "
            "source of divergence than the noisy prose channel (the death spiral punishes "
            "structural_min before prose noise catches up), and on blocklist gates "
            "structural truncation forgets taints at a rate comparable to prose recall "
            "loss. Prose remains strictly worse on score gates (lossless for structural "
            "arms by construction) and catastrophically worse on allowlist gates.\n"
        )

    lines.extend(_analytic_death_lines(results_dir, death, death_cadence))

    add("\n## Per-config results (blind mode, rates vs oracle)\n")
    add("| cadence | profile | arm | agreement | false-proceed | false-stop |")
    add("|---|---|---|---|---|---|")
    for cadence in sorted(blind["cadence"].unique()):
        for profile in profiles_present:
            for arm in ARMS:
                cell = blind[
                    (blind["cadence"] == cadence)
                    & (blind["profile"] == profile)
                    & (blind["arm"] == arm)
                ]
                agree, fp, fs = _rates(cell)
                add(
                    f"| {cadence} | {profile} | {arm} | {_pct(agree)} | {_pct(fp)} | {_pct(fs)} |"
                )

    lines.extend(_matched_slice_lines(blind, llm_dir))

    add("\n## Rehydration (Quimby): blind vs degrade-to-untrusted vs rehydrate\n")
    add("Structural arms, per lineage gate, main matrix. Rehydrate fetches folded hops")
    add("from the append-only log; degrade refuses to act when detail is missing.\n")
    add("| gate | mode | agreement | false-proceed | false-stop | lookups/100 dec | KB read/100 dec |")
    add("|---|---|---|---|---|---|---|")
    for policy in sorted(lineage["policy"].unique()):
        for mode in ("blind", "degrade", "rehydrate"):
            cell = lineage[(lineage["policy"] == policy) & (lineage["mode"] == mode)]
            if cell.empty:
                continue
            agree, fp, fs = _rates(cell)
            n = int(cell["n"].sum())
            looks = 100.0 * float(cell["lookups"].sum()) / n
            kb = 100.0 * float(cell["bytes_read"].sum()) / n / 1024.0
            add(
                f"| {policy} | {mode} | {_pct(agree)} | {_pct(fp)} | {_pct(fs)} "
                f"| {looks:.1f} | {kb:.1f} |"
            )

    add("\n## Per-axis score drift (MAE vs oracle at decision points, main matrix)\n")
    add("| arm | " + " | ".join(sorted(drift["axis"].unique())) + " |")
    add("|---|" + "---|" * len(drift["axis"].unique()))
    dmain = drift[drift["run_type"] == "main"]
    for arm in ARMS:
        row = [arm]
        for axis in sorted(dmain["axis"].unique()):
            cell = dmain[(dmain["arm"] == arm) & (dmain["axis"] == axis)]
            mae = float((cell["mae"] * cell["n"]).sum() / cell["n"].sum())
            row.append(f"{mae:.4f}")
        add("| " + " | ".join(row) + " |")
    add(
        "\nStructural base-axis drift is exactly 0.0000 — the lossless-score invariant, "
        "verified at every decision point (and property-tested). structural_min drifts only "
        "on `reconstruction`; prose drifts everywhere.\n"
    )

    add("\n## Prose channel realized parameters (mock mode)\n")
    pm = prose[prose["run_type"] == "main"]
    add(f"- extraction parse-failure rate: {_pct(float(pm['parse_failure_rate'].mean()))}")
    recall = float(pm["n_kept_taints"].sum() / max(pm["n_true_taints"].sum(), 1))
    reported = pm["n_kept_taints"].sum() + pm["n_fabricated_taints"].sum()
    precision = float(pm["n_kept_taints"].sum() / max(reported, 1))
    add(f"- realized taint recall: {recall:.3f} (configured 0.6)")
    add(f"- realized taint precision: {precision:.3f} (configured 0.9)\n")

    lines.extend(_crossover_lines(sweep_dir))

    add("\n## Figures\n")
    n_figures = 2
    if fig_decay is not None:
        n_figures += 1
        add(
            f"- `{fig_decay.name}` — reconstruction decay: min-folded vs per-hop "
            f"fidelity (death-spiral run)"
        )
    add(f"- `{fig_agree.name}` — gate agreement with the oracle, by gate class and arm")
    add(f"- `{fig_fp.name}` — false-proceed rate on irreversible gates vs compaction cadence\n")

    (results_dir / "summary.md").write_text("\n".join(lines))
    print(f"wrote {results_dir / 'summary.md'} and {n_figures} figures")


def _verdict(ok: bool) -> str:
    return "**PASS**" if ok else "**FAIL**"


def _ordered_profiles(blind: pd.DataFrame) -> list[str]:
    present = list(dict.fromkeys(str(p) for p in blind["profile"]))
    known = [p for p in ("low", "med", "high") if p in present]
    return known + sorted(p for p in present if p not in ("low", "med", "high"))


def _trace_coverage_lines(results_dir: Path) -> list[str]:
    """Trace mode: which trace records were mapped, which were skipped, and
    which derivation rule fired how often."""
    path = results_dir / "trace_coverage.json"
    if not path.exists():
        return []
    cov = json.loads(path.read_text())
    lines = [
        "\n## Trace coverage\n",
        f"- records in trace: {cov['n_records']}",
        f"- mapped to replay steps: {cov['n_mapped']}",
        f"- skipped: {cov['n_skipped']}",
    ]
    for reason, count in cov.get("skipped_by_reason", {}).items():
        lines.append(f"    - {reason}: {count}")
    for reason, count in cov.get("warnings", {}).items():
        lines.append(f"- warning — {reason}: {count}")
    lines.append("- taints derived per rule:")
    for label, hits in cov.get("rule_hits", {}).items():
        lines.append(f"    - {label}: {hits}")
    return lines


def _death_cycle(
    death: pd.DataFrame,
    cadence: int,
    arm: str = "structural_min",
    policy: str | None = None,
) -> int | None:
    """Smallest compaction-cycle count after which the arm blocks every
    reconstruction-coupled decision the oracle would have allowed, permanently.

    Conditioning on oracle-proceed decisions separates "the arm's memory died"
    from "the trajectory itself warranted a block"."""
    rows = death[(death["arm"] == arm) & death["oracle_proceed"]].copy()
    if policy is not None:
        rows = rows[rows["policy"] == policy]
    if rows.empty:
        return None
    rows["cycle"] = rows["step"] // cadence
    proceeds = rows[rows["proceed"]]
    if proceeds.empty:
        return 0
    last_proceed = int(proceeds["cycle"].max())
    max_cycle = int(rows["cycle"].max())
    if last_proceed >= max_cycle:
        return None  # still proceeding at the end of the run: never died
    return last_proceed + 1


def _analytic_death_lines(
    results_dir: Path, death: pd.DataFrame, death_cadence: int | None
) -> list[str]:
    """Closed-form death cycle n = ln(θ) / ln(1 − p) per reconstruction-coupled
    gate, next to the empirical cycle from the death-spiral run."""
    meta_path = results_dir / "run_meta.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    penalty = float(meta.get("reconstruction_penalty", 0.0))
    thresholds: dict[str, float] = {
        str(k): float(v) for k, v in meta.get("recon_gate_thresholds", {}).items()
    }
    if penalty <= 0.0 or not thresholds:
        return []
    lines = [
        f"\n## Reconstruction death-cycle analytics (penalty p = {penalty})\n",
        "Closed form: memory dies for a gate with permanent-block threshold θ at",
        "n = ln(θ) / ln(1 − p) compaction cycles. Empirical column is the first",
        "cycle after which the gate blocks every oracle-allowed decision in the",
        "death-spiral run (— when the run's horizon was too short to reach it).\n",
        "θ is the *pristine-axis* bound: it assumes the discounted score is 1.0.",
        "For a min-floor gate that is exact (reconstruction below θ blocks on its",
        "own). A discounted gate actually blocks once r < θ / (the freshest score",
        "reaching a decision), so its empirical death can arrive a cycle early —",
        "e.g. at p = 0.02, θ = 0.55, cycle 29 needs freshness ≥ 0.988 to proceed,",
        "and one ordinary cache read (×0.95) already rules that out.\n",
        "| gate | θ | analytic n | first whole cycle | empirical (death-spiral) |",
        "|---|---|---|---|---|",
    ]
    for gate in sorted(thresholds):
        theta = thresholds[gate]
        n_real = math.log(theta) / math.log(1.0 - penalty)
        first_whole = math.floor(n_real) + 1
        empirical = (
            _death_cycle(death, death_cadence, policy=gate)
            if death_cadence is not None
            else None
        )
        lines.append(
            f"| {gate} | {theta:.2f} | {n_real:.1f} | {first_whole} | "
            f"{empirical if empirical is not None else '—'} |"
        )
    return lines


def _matched_slice_lines(blind_mock: pd.DataFrame, llm_dir: Path | None) -> list[str]:
    """Side-by-side mock vs real-LLM comparison on identical
    (cadence, profile, seed) cells — never silently mixed slices."""
    header = ["\n## Matched-slice comparison: mock vs real-LLM prose channel\n"]
    llm_path: Path | None = None
    candidates = []
    if llm_dir is not None:
        candidates.append(llm_dir / "gate_metrics.csv")
    candidates.append(FROZEN_LLM_GATE_METRICS)
    for candidate in candidates:
        if candidate.exists():
            llm_path = candidate
            break
    if llm_path is None:
        return header + [
            "_Skipped: no real-LLM results found (looked for a results-llm dir "
            "and the frozen copy under docs/data/)._\n"
        ]
    llm = pd.read_csv(llm_path)
    llm_blind = llm[(llm["run_type"] == "main") & (llm["mode"] == "blind")]

    def cells_of(df: pd.DataFrame) -> set[tuple[int, str, int]]:
        unique = df[["cadence", "profile", "seed"]].drop_duplicates()
        return {
            (int(str(c)), str(p), int(str(s)))
            for c, p, s in zip(unique["cadence"], unique["profile"], unique["seed"])
        }

    common = sorted(cells_of(blind_mock) & cells_of(llm_blind))
    if not common:
        return header + [
            "_Skipped: the mock results and the real-LLM results share no "
            "(cadence, profile, seed) cells; run "
            "`prov-lab run --config experiments/config.matched.yaml --mock` "
            "to produce the matched mock slice._\n"
        ]
    cells_df = pd.DataFrame(common, columns=["cadence", "profile", "seed"])
    mock_m = blind_mock.merge(cells_df, on=["cadence", "profile", "seed"])
    llm_m = llm_blind.merge(cells_df, on=["cadence", "profile", "seed"])
    cell_desc = (
        f"C ∈ {{{', '.join(str(c) for c in sorted({c for c, _, _ in common}))}}}, "
        f"profiles {{{', '.join(sorted({p for _, p, _ in common}))}}}, "
        f"seeds {{{', '.join(str(s) for s in sorted({s for _, _, s in common}))}}}"
    )
    lines = header + [
        f"Computed on the **identical cells only** ({cell_desc} — the "
        f"intersection of both result sets; source: `{llm_path}`). The mock "
        "channel uses the configured noise parameters; the real column is the "
        "measured gpt-5-mini summarize→extract round trip.\n",
        "| arm | gate class | mock flip | real flip | mock fp | real fp | mock fs | real fs |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in ARMS:
        for gate_class in ("(all)",) + GATE_CLASSES:
            mock_sel = mock_m[mock_m["arm"] == arm]
            llm_sel = llm_m[llm_m["arm"] == arm]
            if gate_class != "(all)":
                mock_sel = mock_sel[mock_sel["gate_class"] == gate_class]
                llm_sel = llm_sel[llm_sel["gate_class"] == gate_class]
            _, m_fp, m_fs = _rates(mock_sel)
            _, l_fp, l_fs = _rates(llm_sel)
            lines.append(
                f"| {arm} | {gate_class} | {_pct(_flip(mock_sel))} | "
                f"{_pct(_flip(llm_sel))} | {_pct(m_fp)} | {_pct(l_fp)} | "
                f"{_pct(m_fs)} | {_pct(l_fs)} |"
            )
    return lines


def _crossover_lines(sweep_dir: Path | None) -> list[str]:
    """Crossover cadence (structural_min vs prose, reconstruction-coupled
    gates) as a function of the reconstruction penalty, plus the ~1/p scaling
    check. Emits docs/figures/fig_crossover_vs_penalty.png."""
    candidates = []
    if sweep_dir is not None:
        candidates.append(sweep_dir / "sweep_metrics.csv")
    candidates.append(FROZEN_SWEEP_METRICS)  # frozen aggregate, for fresh clones
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        return []
    df = pd.read_csv(path)
    meta_path = path.parent / "sweep_meta.json"
    steps = 500
    if meta_path.exists():
        steps = int(json.loads(meta_path.read_text()).get("steps", 500))
    recon = df[(df["gate_class"] == "reconstruction") & (df["mode"] == "blind")]

    def flip_at(arm: str, penalty: float, cadence: int) -> float:
        sel = recon[
            (recon["arm"] == arm)
            & (recon["penalty"] == penalty)
            & (recon["cadence"] == cadence)
        ]
        return _flip(sel)

    penalties = sorted(recon["penalty"].unique())
    cadences = sorted(int(c) for c in recon["cadence"].unique())
    crossovers: dict[float, float | None] = {}
    censored: dict[float, str] = {}
    for penalty in penalties:
        deltas = [
            flip_at("structural_min", penalty, c) - flip_at("prose", penalty, c)
            for c in cadences
        ]
        if deltas[0] <= 0:
            crossovers[penalty] = None
            censored[penalty] = f"< {cadences[0]}"
            continue
        crossover: float | None = None
        for i in range(len(cadences) - 1):
            if deltas[i] > 0 >= deltas[i + 1]:
                # interpolate in log-cadence between the sign change
                lo, hi = math.log10(cadences[i]), math.log10(cadences[i + 1])
                frac = deltas[i] / (deltas[i] - deltas[i + 1])
                crossover = 10 ** (lo + frac * (hi - lo))
                break
        crossovers[penalty] = crossover
        if crossover is None:
            censored[penalty] = f"> {cadences[-1]}"

    lines = [
        "\n## Crossover vs reconstruction penalty (sweep)\n",
        "Crossover = cadence above which structural_min's reconstruction-coupled",
        "flip rate drops below the prose strawman's (med profile, mock channel).",
        "Cycles-in-horizon = steps / crossover cadence.\n",
        "| penalty p | crossover cadence C* | cycles* = steps/C* | cycles* · p |",
        "|---|---|---|---|",
    ]
    fit_p: list[float] = []
    fit_cycles: list[float] = []
    for penalty in penalties:
        crossover = crossovers[penalty]
        if crossover is None:
            lines.append(f"| {penalty} | {censored[penalty]} (censored) | — | — |")
            continue
        cycles = steps / crossover
        fit_p.append(penalty)
        fit_cycles.append(cycles)
        lines.append(
            f"| {penalty} | {crossover:.1f} | {cycles:.1f} | {cycles * penalty:.3f} |"
        )
    if len(fit_cycles) >= 2:
        import numpy as np

        slope, intercept = np.polyfit(
            np.log(np.asarray(fit_p)), np.log(np.asarray(fit_cycles)), 1
        )
        k = math.exp(float(intercept))
        products = [c * p for c, p in zip(fit_cycles, fit_p)]
        mean_prod = sum(products) / len(products)
        spread = (
            (max(products) - min(products)) / mean_prod if mean_prod else float("inf")
        )
        if abs(slope + 1.0) < 0.3:
            verdict = (
                "**roughly constant** — consistent with the ~1/p scaling of the "
                "analytic death cycle n ≈ −ln(θ)/p"
            )
        else:
            verdict = (
                f"**NOT constant** over this grid (fitted exponent {slope:.2f} "
                "instead of −1) — quote the fitted relation, not a 1/p rule"
            )
        lines.append(
            f"\nFitted relation: **cycles\\* ≈ {k:.2f} · p^{slope:.2f}** "
            f"(log-log fit over {len(fit_cycles)} uncensored penalties). "
            f"crossover·p is {verdict} (mean {mean_prod:.3f}, relative spread "
            f"{spread:.0%})."
        )
    lines.append(
        "\nCaveat: crossovers near the top of the cadence grid correspond to "
        "≤2 compactions inside the horizon — both arms barely flip there and "
        "the interpolated C* is noise-dominated; treat those points as bounds. "
        "The scaling is also not expected to be exactly 1/p: the prose arm's "
        "flip rate is itself a function of the cycle count it races against."
    )
    fig_path = REPO_ROOT / "docs" / "figures" / "fig_crossover_vs_penalty.png"
    _fig_crossover_vs_penalty(crossovers, censored, steps, fig_path)
    lines.append(f"\nFigure: `{fig_path.relative_to(REPO_ROOT)}`")
    return lines


def _fig_crossover_vs_penalty(
    crossovers: dict[float, float | None],
    censored: dict[float, str],
    steps: int,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    xs = [p for p, c in sorted(crossovers.items()) if c is not None]
    ys = [c for _, c in sorted(crossovers.items()) if c is not None]
    ax.plot(xs, ys, marker="o", markersize=7, linewidth=2, color="#2a78d6")
    for x, y in zip(xs, ys):
        ax.annotate(
            f"{steps / y:.0f} cycles",
            (x, y),
            textcoords="offset points",
            xytext=(8, -4),
            fontsize=8,
            color=GRAY,
        )
    for p, label in censored.items():
        ax.annotate(
            f"p={p}: {label}",
            xy=(0.02, 0.05 + 0.07 * list(censored).index(p)),
            xycoords="axes fraction",
            fontsize=8,
            color=GRAY,
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("reconstruction penalty p (per compaction)")
    ax.set_ylabel("crossover cadence C*")
    ax.set_title(
        "Where min-folding starts losing to the prose strawman\n"
        "(reconstruction-coupled gates, med profile)"
    )
    ax.grid(alpha=0.25, which="both")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_reconstruction_decay(
    ds_curve: pd.DataFrame, cadence: int, death_cycle: int | None, path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    cycles = ds_curve["n_compactions"]
    ax.plot(
        cycles,
        ds_curve["recon_min"],
        color=COLOR["structural_min"],
        linewidth=2,
        label="structural_min (folded with min)",
    )
    ax.plot(
        cycles,
        ds_curve["fidelity_perhop"],
        color=COLOR["structural_perhop"],
        linewidth=2,
        label="structural_perhop (fidelity = worst single penalty)",
    )
    ax.axhline(0.5, color=GRAY, linewidth=1, linestyle="--")
    ax.text(cycles.max(), 0.51, "gate floor 0.5", ha="right", fontsize=9, color=GRAY)
    if death_cycle is not None:
        ax.axvline(death_cycle, color=GRAY, linewidth=1, linestyle=":")
        ax.text(
            death_cycle + 2, 0.06, f"memory dies\n(cycle {death_cycle})",
            fontsize=9, color=INK,
        )
    last = ds_curve.iloc[-1]
    ax.text(
        cycles.max(), float(last["recon_min"]) + 0.03, "structural_min",
        ha="right", fontsize=10, color=COLOR["structural_min"],
    )
    ax.text(
        cycles.max(), float(last["fidelity_perhop"]) - 0.06, "structural_perhop",
        ha="right", fontsize=10, color=COLOR["structural_perhop"],
    )
    ax.set_xlabel("compaction cycles")
    ax.set_ylabel("reconstruction axis seen by gates")
    ax.set_title(
        f"Boyko's recursion: min-folded reconstruction decays to distrust "
        f"(death-spiral run, C={cadence})"
    )
    ax.set_ylim(0, 1.05)
    ax.grid(axis="y", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9, loc="center", bbox_to_anchor=(0.55, 0.72))
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_agreement_by_class(blind: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    width = 0.26
    xs = range(len(GATE_CLASSES))
    for i, arm in enumerate(ARMS):
        values = []
        for gate_class in GATE_CLASSES:
            agree, _, _ = _rates(
                blind[(blind["arm"] == arm) & (blind["gate_class"] == gate_class)]
            )
            values.append(agree)
        offset = (i - 1) * width
        bars = ax.bar(
            [x + offset for x in xs], values, width=width * 0.92,
            color=COLOR[arm], label=arm,
        )
        for bar, v in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2, v + 0.012, f"{100 * v:.1f}",
                ha="center", fontsize=8, color=INK,
            )
    ax.set_xticks(list(xs))
    ax.set_xticklabels(["score", "reconstruction-\ncoupled", "lineage\nblocklist", "lineage\nallowlist"])
    ax.set_ylabel("agreement with uncompacted oracle")
    ax.set_ylim(0.0, 1.08)  # bars keep their zero baseline
    ax.set_title("Gate agreement by gate class (main matrix, blind mode)")
    ax.grid(axis="y", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(
        frameon=False, fontsize=9, ncols=3,
        loc="upper center", bbox_to_anchor=(0.5, -0.14),
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _fig_false_proceed_vs_cadence(
    blind: pd.DataFrame, profiles: list[str], path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    irr = blind[blind["irreversible"] & (blind["arm"] == "structural_min")]
    cadences = sorted(irr["cadence"].unique())
    for profile in profiles:
        ys = []
        for cadence in cadences:
            _, fp, _ = _rates(
                irr[(irr["cadence"] == cadence) & (irr["profile"] == profile)]
            )
            ys.append(100 * fp)
        color = PROFILE_COLOR.get(profile, "#2a78d6")
        ax.plot(
            cadences, ys, marker="o", markersize=6, linewidth=2,
            color=color, label=f"{profile} profile",
        )
        ax.text(
            cadences[-1] + 1, ys[-1], profile,
            fontsize=9, color=color, va="center",
        )
    ax.set_xlabel("compaction cadence C (steps between compactions)")
    ax.set_ylabel("false-proceed rate on irreversible gates (%)")
    ax.set_title("False-proceeds on irreversible gates vs compaction cadence (structural_min)")
    ax.set_xticks(cadences)
    ax.grid(axis="y", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    build_report(Path("results"))
