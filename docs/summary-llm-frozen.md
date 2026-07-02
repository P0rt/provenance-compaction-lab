# Provenance under compaction — experiment summary

## HEADLINE NUMBERS

1. **False-proceed rate on irreversible gates** (`structural_min`, C=25, med profile): **5.50%** of irreversible-action decisions proceeded when the uncompacted oracle said block.

2. **structural_min memory did not die within this run's horizon** (death-spiral run, C=25) — too few compaction cycles to cross the 0.5 gate floor; see the default config for the full run.

3. **Prose-vs-structural flip-rate ratio: 0.89×** (prose 10.89% vs structural_min 12.25% decisions flipped vs the oracle, all gates, main matrix).

4. **Rehydration**: 758 cold-storage lookups per 100 lineage-gate decisions buy a **16.75% absolute flip-rate reduction** (blind 16.75% → rehydrate 0.00%).


## Hypothesis verdicts

| Hypothesis | Verdict | Evidence |
|---|---|---|
| **H1 (Boyko)** — score gates agree 100% between oracle and structural arms; lineage gates carry the divergence | **PASS** | score-gate flip rate 0.00% (**by construction, not a discovery** — compaction never touches the running min of the base axes); lineage-gate flip rate 16.75% |
| **H2 (death spiral)** — min-folded reconstruction decays monotonically and eventually blocks ALL reconstruction-coupled gates permanently; perhop does not | **FAIL** | monotone: True; death cycle: None; perhop dies: False |
| **H3 (error direction is a design choice)** — blocklist → false-proceeds, allowlist → false-stops | **PASS** | blocklist fp 21.00% vs fs 0.00%; allowlist fs 12.50% vs fp 0.00% (structural_min) |
| **H4 (prose is catastrophic)** — prose flip rates dominate structural arms on every gate class | **FAIL** | see per-class table below |

### H4 per gate class (flip rate vs oracle, main matrix, blind)

| gate class | prose | structural_min | structural_perhop | prose dominates? |
|---|---|---|---|---|
| score | 0.08% | 0.00% | 0.00% | yes |
| reconstruction | 0.00% | 21.63% | 0.00% | **no** |
| lineage_blocklist | 5.75% | 21.00% | 21.00% | **no** |
| lineage_allowlist | 43.12% | 12.50% | 12.50% | yes |

> H4 **fails** as stated, and the failure is itself a finding: on reconstruction-coupled gates the min-folded reconstruction axis is a *bigger* source of divergence than the noisy prose channel (the death spiral punishes structural_min before prose noise catches up), and on blocklist gates structural truncation forgets taints at a rate comparable to prose recall loss. Prose remains strictly worse on score gates (lossless for structural arms by construction) and catastrophically worse on allowlist gates.


## Per-config results (blind mode, rates vs oracle)

| cadence | profile | arm | agreement | false-proceed | false-stop |
|---|---|---|---|---|---|
| 10 | low | structural_min | nan% | nan% | nan% |
| 10 | low | structural_perhop | nan% | nan% | nan% |
| 10 | low | prose | nan% | nan% | nan% |
| 10 | med | structural_min | 82.94% | 5.39% | 11.67% |
| 10 | med | structural_perhop | 90.61% | 5.39% | 4.00% |
| 10 | med | prose | 84.44% | 2.00% | 13.56% |
| 10 | high | structural_min | nan% | nan% | nan% |
| 10 | high | structural_perhop | nan% | nan% | nan% |
| 10 | high | prose | nan% | nan% | nan% |
| 25 | low | structural_min | nan% | nan% | nan% |
| 25 | low | structural_perhop | nan% | nan% | nan% |
| 25 | low | prose | nan% | nan% | nan% |
| 25 | med | structural_min | 92.56% | 3.94% | 3.50% |
| 25 | med | structural_perhop | 94.50% | 3.94% | 1.56% |
| 25 | med | prose | 93.78% | 0.56% | 5.67% |
| 25 | high | structural_min | nan% | nan% | nan% |
| 25 | high | structural_perhop | nan% | nan% | nan% |
| 25 | high | prose | nan% | nan% | nan% |

## Rehydration (Quimby): blind vs degrade-to-untrusted vs rehydrate

Structural arms, per lineage gate, main matrix. Rehydrate fetches folded hops
from the append-only log; degrade refuses to act when detail is missing.

| gate | mode | agreement | false-proceed | false-stop | lookups/100 dec | KB read/100 dec |
|---|---|---|---|---|---|---|
| audit_requires_clean_window | blind | 87.00% | 0.00% | 13.00% | 0.0 | 0.0 |
| audit_requires_clean_window | degrade | 87.00% | 0.00% | 13.00% | 0.0 | 0.0 |
| audit_requires_clean_window | rehydrate | 100.00% | 0.00% | 0.00% | 504.0 | 61.2 |
| payment_no_untrusted_taint | blind | 80.00% | 20.00% | 0.00% | 0.0 | 0.0 |
| payment_no_untrusted_taint | degrade | 97.25% | 0.00% | 2.75% | 0.0 | 0.0 |
| payment_no_untrusted_taint | rehydrate | 100.00% | 0.00% | 0.00% | 1012.5 | 123.4 |
| send_wire_clean_window | blind | 88.00% | 0.00% | 12.00% | 0.0 | 0.0 |
| send_wire_clean_window | degrade | 88.00% | 0.00% | 12.00% | 0.0 | 0.0 |
| send_wire_clean_window | rehydrate | 100.00% | 0.00% | 0.00% | 504.0 | 61.2 |
| summarize_no_unverified_taint | blind | 78.00% | 22.00% | 0.00% | 0.0 | 0.0 |
| summarize_no_unverified_taint | degrade | 94.75% | 0.00% | 5.25% | 0.0 | 0.0 |
| summarize_no_unverified_taint | rehydrate | 100.00% | 0.00% | 0.00% | 1012.5 | 123.4 |

## Per-axis score drift (MAE vs oracle at decision points, main matrix)

| arm | capability | freshness | reconstruction | tool_integrity | verification |
|---|---|---|---|---|---|
| structural_min | 0.0000 | 0.0000 | 0.2714 | 0.0000 | 0.0000 |
| structural_perhop | 0.0000 | 0.0000 | 0.0195 | 0.0000 | 0.0000 |
| prose | 0.0001 | 0.0001 | 0.0000 | 0.0000 | 0.0000 |

Structural base-axis drift is exactly 0.0000 — the lossless-score invariant, verified at every decision point (and property-tested). structural_min drifts only on `reconstruction`; prose drifts everywhere.


## Prose channel realized parameters (mock mode)

- extraction parse-failure rate: 0.00%
- realized taint recall: 0.893 (configured 0.6)
- realized taint precision: 0.904 (configured 0.9)


## Figures

- `fig_reconstruction_decay.png` — reconstruction decay: min-folded vs per-hop fidelity (death-spiral run)
- `fig_gate_agreement_by_class.png` — gate agreement with the oracle, by gate class and arm
- `fig_false_proceed_vs_cadence.png` — false-proceed rate on irreversible gates vs compaction cadence
