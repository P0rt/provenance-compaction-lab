"""Build results/summary.md + figures from the aggregate CSVs.

Reads:  results/gate_metrics.csv, drift.csv, recon_curve.csv,
        prose_channel.csv, death_spiral_decisions.csv
Writes: results/summary.md, results/fig_*.png
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

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


def build_report(results_dir: Path) -> None:
    gates = pd.read_csv(results_dir / "gate_metrics.csv")
    drift = pd.read_csv(results_dir / "drift.csv")
    curve = pd.read_csv(results_dir / "recon_curve.csv")
    prose = pd.read_csv(results_dir / "prose_channel.csv")
    death = pd.read_csv(results_dir / "death_spiral_decisions.csv")

    main = gates[gates["run_type"] == "main"]
    blind = main[main["mode"] == "blind"]
    death_cadence = int(gates[gates["run_type"] == "death_spiral"]["cadence"].iloc[0])

    # ---- headline numbers --------------------------------------------------
    h1_slice = blind[
        (blind["arm"] == "structural_min")
        & blind["irreversible"]
        & (blind["cadence"] == 25)
        & (blind["profile"] == "med")
    ]
    _, headline_fp, _ = _rates(h1_slice)

    death_cycle = _death_cycle(death, death_cadence)
    perhop_dies = _death_cycle(death, death_cadence, arm="structural_perhop") is not None

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
    fig_decay = results_dir / "fig_reconstruction_decay.png"
    _fig_reconstruction_decay(ds_curve, death_cadence, death_cycle, fig_decay)
    fig_agree = results_dir / "fig_gate_agreement_by_class.png"
    _fig_agreement_by_class(blind, fig_agree)
    fig_fp = results_dir / "fig_false_proceed_vs_cadence.png"
    _fig_false_proceed_vs_cadence(blind, fig_fp)

    # ---- summary.md ----------------------------------------------------------
    lines: list[str] = []
    add = lines.append
    add("# Provenance under compaction — experiment summary\n")
    add("## HEADLINE NUMBERS\n")
    add(
        f"1. **False-proceed rate on irreversible gates** (`structural_min`, C=25, med profile): "
        f"**{_pct(headline_fp)}** of irreversible-action decisions proceeded when the "
        f"uncompacted oracle said block.\n"
    )
    add(
        f"2. **structural_min memory dies at compaction cycle ≈ {death_cycle}** "
        f"(death-spiral run, C={death_cadence}): from that cycle on, every "
        f"reconstruction-coupled gate blocks permanently (0.98^{death_cycle} ≈ "
        f"{0.98 ** (death_cycle or 0):.3f} < 0.5). `structural_perhop` "
        f"{'ALSO dies — investigate' if perhop_dies else 'never dies'}.\n"
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
        f"{_verdict(h2_pass)} | monotone: {monotone}; death cycle: {death_cycle}; "
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

    add("\n## Per-config results (blind mode, rates vs oracle)\n")
    add("| cadence | profile | arm | agreement | false-proceed | false-stop |")
    add("|---|---|---|---|---|---|")
    for cadence in sorted(blind["cadence"].unique()):
        for profile in ("low", "med", "high"):
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

    add("\n## Figures\n")
    add(f"- `{fig_decay.name}` — reconstruction decay: min-folded vs per-hop fidelity (death-spiral run)")
    add(f"- `{fig_agree.name}` — gate agreement with the oracle, by gate class and arm")
    add(f"- `{fig_fp.name}` — false-proceed rate on irreversible gates vs compaction cadence\n")

    (results_dir / "summary.md").write_text("\n".join(lines))
    print(f"wrote {results_dir / 'summary.md'} and 3 figures")


def _verdict(ok: bool) -> str:
    return "**PASS**" if ok else "**FAIL**"


def _death_cycle(
    death: pd.DataFrame, cadence: int, arm: str = "structural_min"
) -> int | None:
    """Smallest compaction-cycle count after which the arm blocks every
    reconstruction-coupled decision the oracle would have allowed, permanently.

    Conditioning on oracle-proceed decisions separates "the arm's memory died"
    from "the trajectory itself warranted a block"."""
    rows = death[(death["arm"] == arm) & death["oracle_proceed"]].copy()
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


def _fig_false_proceed_vs_cadence(blind: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    irr = blind[blind["irreversible"] & (blind["arm"] == "structural_min")]
    cadences = sorted(irr["cadence"].unique())
    for profile in ("low", "med", "high"):
        ys = []
        for cadence in cadences:
            _, fp, _ = _rates(
                irr[(irr["cadence"] == cadence) & (irr["profile"] == profile)]
            )
            ys.append(100 * fp)
        ax.plot(
            cadences, ys, marker="o", markersize=6, linewidth=2,
            color=PROFILE_COLOR[profile], label=f"{profile} profile",
        )
        ax.text(
            cadences[-1] + 1, ys[-1], profile,
            fontsize=9, color=PROFILE_COLOR[profile], va="center",
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
