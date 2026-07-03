# Provenance under compaction — experiment summary

## HEADLINE NUMBERS

1. **False-proceed rate on irreversible gates** (`structural_min`, C=25, med profile): **3.47%** of irreversible-action decisions proceeded when the uncompacted oracle said block.

2. **structural_min memory dies at compaction cycle ≈ 35** (death-spiral run, C=25): from that cycle on, every reconstruction-coupled gate blocks permanently (0.98^35 ≈ 0.493 < 0.5). `structural_perhop` never dies.

3. **Prose-vs-structural flip-rate ratio: 1.39×** (prose 9.97% vs structural_min 7.16% decisions flipped vs the oracle, all gates, main matrix).

4. **Rehydration**: 459 cold-storage lookups per 100 lineage-gate decisions buy a **8.01% absolute flip-rate reduction** (blind 8.01% → rehydrate 0.00%).


## Hypothesis verdicts

| Hypothesis | Verdict | Evidence |
|---|---|---|
| **H1 (Boyko)** — score gates agree 100% between oracle and structural arms; lineage gates carry the divergence | **PASS** | score-gate flip rate 0.00% (**by construction, not a discovery** — compaction never touches the running min of the base axes); lineage-gate flip rate 8.01% |
| **H2 (death spiral)** — min-folded reconstruction decays monotonically and eventually blocks ALL reconstruction-coupled gates permanently; perhop does not | **PASS** | monotone: True; death cycle: 35; perhop dies: False |
| **H3 (error direction is a design choice)** — blocklist → false-proceeds, allowlist → false-stops | **PASS** | blocklist fp 8.50% vs fs 0.00%; allowlist fs 7.53% vs fp 0.00% (structural_min) |
| **H4 (prose is catastrophic)** — prose flip rates dominate structural arms on every gate class | **FAIL** | see per-class table below |

### H4 per gate class (flip rate vs oracle, main matrix, blind)

| gate class | prose | structural_min | structural_perhop | prose dominates? |
|---|---|---|---|---|
| score | 2.44% | 0.00% | 0.00% | yes |
| reconstruction | 4.97% | 16.17% | 0.00% | **no** |
| lineage_blocklist | 6.82% | 8.50% | 8.50% | **no** |
| lineage_allowlist | 29.41% | 7.53% | 7.53% | yes |

> H4 **fails** as stated, and the failure is itself a finding: on reconstruction-coupled gates the min-folded reconstruction axis is a *bigger* source of divergence than the noisy prose channel (the death spiral punishes structural_min before prose noise catches up), and on blocklist gates structural truncation forgets taints at a rate comparable to prose recall loss. Prose remains strictly worse on score gates (lossless for structural arms by construction) and catastrophically worse on allowlist gates.


## Reconstruction death-cycle analytics (penalty p = 0.02)

Closed form: memory dies for a gate with permanent-block threshold θ at
n = ln(θ) / ln(1 − p) compaction cycles. Empirical column is the first
cycle after which the gate blocks every oracle-allowed decision in the
death-spiral run (— when the run's horizon was too short to reach it).

| gate | θ | analytic n | first whole cycle | empirical (death-spiral) |
|---|---|---|---|---|
| archive_all_axes_floor | 0.50 | 34.3 | 35 | 35 |
| publish_discounted_thresholds | 0.55 | 29.6 | 30 | 29 |

## Per-config results (blind mode, rates vs oracle)

| cadence | profile | arm | agreement | false-proceed | false-stop |
|---|---|---|---|---|---|
| 10 | low | structural_min | 86.09% | 1.31% | 12.59% |
| 10 | low | structural_perhop | 94.22% | 1.31% | 4.47% |
| 10 | low | prose | 80.82% | 2.08% | 17.09% |
| 10 | med | structural_min | 85.76% | 3.28% | 10.97% |
| 10 | med | structural_perhop | 93.52% | 3.28% | 3.21% |
| 10 | med | prose | 82.18% | 3.79% | 14.03% |
| 10 | high | structural_min | 86.58% | 3.98% | 9.44% |
| 10 | high | structural_perhop | 93.92% | 3.98% | 2.10% |
| 10 | high | prose | 84.30% | 4.29% | 11.41% |
| 25 | low | structural_min | 96.21% | 0.88% | 2.92% |
| 25 | low | structural_perhop | 97.48% | 0.88% | 1.64% |
| 25 | low | prose | 90.74% | 1.28% | 7.98% |
| 25 | med | structural_min | 94.54% | 2.27% | 3.19% |
| 25 | med | structural_perhop | 96.58% | 2.27% | 1.15% |
| 25 | med | prose | 91.59% | 2.12% | 6.29% |
| 25 | high | structural_min | 94.26% | 2.15% | 3.59% |
| 25 | high | structural_perhop | 97.15% | 2.15% | 0.70% |
| 25 | high | prose | 92.88% | 1.96% | 5.16% |
| 50 | low | structural_min | 98.11% | 0.47% | 1.42% |
| 50 | low | structural_perhop | 98.70% | 0.47% | 0.83% |
| 50 | low | prose | 95.50% | 0.64% | 3.86% |
| 50 | med | structural_min | 97.05% | 1.48% | 1.47% |
| 50 | med | structural_perhop | 97.94% | 1.48% | 0.58% |
| 50 | med | prose | 95.76% | 1.01% | 3.23% |
| 50 | high | structural_min | 97.01% | 1.18% | 1.82% |
| 50 | high | structural_perhop | 98.43% | 1.18% | 0.39% |
| 50 | high | prose | 96.50% | 1.05% | 2.45% |

## Matched-slice comparison: mock vs real-LLM prose channel

Computed on the **identical cells only** (C ∈ {10, 25}, profiles {med}, seeds {0, 1} — the intersection of both result sets; source: `results-llm/gate_metrics.csv`). The mock channel uses the configured noise parameters; the real column is the measured gpt-5-mini summarize→extract round trip.

| arm | gate class | mock flip | real flip | mock fp | real fp | mock fs | real fs |
|---|---|---|---|---|---|---|---|
| structural_min | (all) | 12.25% | 12.25% | 4.67% | 4.67% | 7.58% | 7.58% |
| structural_min | score | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| structural_min | reconstruction | 21.63% | 21.63% | 0.00% | 0.00% | 21.62% | 21.62% |
| structural_min | lineage_blocklist | 21.00% | 21.00% | 21.00% | 21.00% | 0.00% | 0.00% |
| structural_min | lineage_allowlist | 12.50% | 12.50% | 0.00% | 0.00% | 12.50% | 12.50% |
| structural_perhop | (all) | 7.44% | 7.44% | 4.67% | 4.67% | 2.78% | 2.78% |
| structural_perhop | score | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| structural_perhop | reconstruction | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| structural_perhop | lineage_blocklist | 21.00% | 21.00% | 21.00% | 21.00% | 0.00% | 0.00% |
| structural_perhop | lineage_allowlist | 12.50% | 12.50% | 0.00% | 0.00% | 12.50% | 12.50% |
| prose | (all) | 14.89% | 10.89% | 4.00% | 1.28% | 10.89% | 9.61% |
| prose | score | 1.75% | 0.08% | 0.50% | 0.00% | 1.25% | 0.08% |
| prose | reconstruction | 4.50% | 0.00% | 0.62% | 0.00% | 3.88% | 0.00% |
| prose | lineage_blocklist | 16.75% | 5.75% | 16.62% | 5.75% | 0.12% | 0.00% |
| prose | lineage_allowlist | 43.12% | 43.12% | 0.00% | 0.00% | 43.12% | 43.12% |

## Rehydration (Quimby): blind vs degrade-to-untrusted vs rehydrate

Structural arms, per lineage gate, main matrix. Rehydrate fetches folded hops
from the append-only log; degrade refuses to act when detail is missing.

| gate | mode | agreement | false-proceed | false-stop | lookups/100 dec | KB read/100 dec |
|---|---|---|---|---|---|---|
| audit_requires_clean_window | blind | 91.86% | 0.00% | 8.14% | 0.0 | 0.0 |
| audit_requires_clean_window | degrade | 91.86% | 0.00% | 8.14% | 0.0 | 0.0 |
| audit_requires_clean_window | rehydrate | 100.00% | 0.00% | 0.00% | 278.4 | 33.6 |
| payment_no_untrusted_taint | blind | 91.22% | 8.78% | 0.00% | 0.0 | 0.0 |
| payment_no_untrusted_taint | degrade | 95.72% | 0.00% | 4.28% | 0.0 | 0.0 |
| payment_no_untrusted_taint | rehydrate | 100.00% | 0.00% | 0.00% | 639.9 | 77.7 |
| send_wire_clean_window | blind | 93.08% | 0.00% | 6.92% | 0.0 | 0.0 |
| send_wire_clean_window | degrade | 93.08% | 0.00% | 6.92% | 0.0 | 0.0 |
| send_wire_clean_window | rehydrate | 100.00% | 0.00% | 0.00% | 278.4 | 33.6 |
| summarize_no_unverified_taint | blind | 91.79% | 8.21% | 0.00% | 0.0 | 0.0 |
| summarize_no_unverified_taint | degrade | 92.11% | 0.00% | 7.89% | 0.0 | 0.0 |
| summarize_no_unverified_taint | rehydrate | 100.00% | 0.00% | 0.00% | 639.9 | 77.7 |

## Per-axis score drift (MAE vs oracle at decision points, main matrix)

| arm | capability | freshness | reconstruction | tool_integrity | verification |
|---|---|---|---|---|---|
| structural_min | 0.0000 | 0.0000 | 0.2100 | 0.0000 | 0.0000 |
| structural_perhop | 0.0000 | 0.0000 | 0.0191 | 0.0000 | 0.0000 |
| prose | 0.0286 | 0.0273 | 0.0347 | 0.0313 | 0.0333 |

Structural base-axis drift is exactly 0.0000 — the lossless-score invariant, verified at every decision point (and property-tested). structural_min drifts only on `reconstruction`; prose drifts everywhere.


## Prose channel realized parameters (mock mode)

- extraction parse-failure rate: 0.00%
- realized taint recall: 0.602 (configured 0.6)
- realized taint precision: 0.903 (configured 0.9)


## Crossover vs reconstruction penalty (sweep)

Crossover = cadence above which structural_min's reconstruction-coupled
flip rate drops below the prose strawman's (med profile, mock channel).
Cycles-in-horizon = steps / crossover cadence.

| penalty p | crossover cadence C* | cycles* = steps/C* | cycles* · p |
|---|---|---|---|
| 0.005 | 4.9 | 102.5 | 0.513 |
| 0.01 | 23.6 | 21.2 | 0.212 |
| 0.02 | 69.0 | 7.2 | 0.145 |
| 0.05 | 410.2 | 1.2 | 0.061 |
| 0.1 | 488.9 | 1.0 | 0.102 |

Fitted relation: **cycles\* ≈ 0.02 · p^-1.59** (log-log fit over 5 uncensored penalties). crossover·p is **NOT constant** over this grid (fitted exponent -1.59 instead of −1) — quote the fitted relation, not a 1/p rule (mean 0.206, relative spread 219%).

Caveat: crossovers near the top of the cadence grid correspond to ≤2 compactions inside the horizon — both arms barely flip there and the interpolated C* is noise-dominated; treat those points as bounds. The scaling is also not expected to be exactly 1/p: the prose arm's flip rate is itself a function of the cycle count it races against.

Figure: `docs/figures/fig_crossover_vs_penalty.png`

## Figures

- `fig_reconstruction_decay.png` — reconstruction decay: min-folded vs per-hop fidelity (death-spiral run)
- `fig_gate_agreement_by_class.png` — gate agreement with the oracle, by gate class and arm
- `fig_false_proceed_vs_cadence.png` — false-proceed rate on irreversible gates vs compaction cadence
