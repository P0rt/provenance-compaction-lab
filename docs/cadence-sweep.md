# Cadence sweep: where structural_min vs prose flip rates cross

Med profile, 20 seeds per cadence, 500-step horizon, mock prose channel.
Flip rate = % of decisions that disagree with the uncompacted oracle (blind mode).

| C (steps/compaction) | cycles in horizon | min: all | prose: all | min: reconstruction | prose: reconstruction | min: blocklist | prose: blocklist |
|---|---|---|---|---|---|---|---|
| 5 | 100 | 22.98% | 26.61% | 55.02% | 11.58% | 18.75% | 19.57% |
| 10 | 50 | 14.24% | 17.82% | 34.92% | 6.85% | 14.75% | 13.40% |
| 15 | 33 | 8.22% | 13.61% | 15.53% | 6.55% | 12.10% | 11.80% |
| 20 | 25 | 6.46% | 9.91% | 10.78% | 4.63% | 11.22% | 7.53% |
| 25 | 20 | 5.46% | 8.41% | 9.18% | 4.53% | 10.22% | 7.40% |
| 35 | 14 | 4.20% | 6.24% | 6.75% | 3.40% | 8.00% | 4.55% |
| 50 | 10 | 2.95% | 4.24% | 4.03% | 2.68% | 6.65% | 4.03% |
| 75 | 6 | 1.71% | 2.70% | 1.35% | 1.70% | 4.90% | 2.25% |
| 100 | 5 | 1.10% | 1.96% | 0.42% | 1.00% | 3.27% | 1.85% |

**Crossover (the number for the post):** on reconstruction-coupled gates,
`structural_min` is WORSE than the prose strawman at every cadence up to C=50
(≥10 compaction cycles in the horizon) and only drops below prose between
C=50 and C=75 — i.e. once the memory sees more than **~7–10 compaction
cycles inside a decision horizon, min-folded reconstruction (not
summarization noise) is the dominant corruption source**. On blocklist gates
the cross happens even earlier (C≈15): structural taint-folding forgets more
than the prose channel's 0.6 recall for any realistic cadence. Overall
(all gates pooled) prose stays worse than structural_min at every cadence —
the allowlist starvation and score noise keep it behind.
