# Worked examples for the post

Concrete anchors behind the aggregate rates. All reproducible: seed 0,
C=25, med profile, default config.

## 1. Anatomy of one false-proceed (the headline failure)

Run: seed 0, cadence C=25, med profile. Decision at **step 25**, gate
`payment_no_untrusted_taint` (irreversible; blocks on `unverified_web` /
`tool_flaky` taints).

The working value's full taint history by step 25 (what the uncompacted
oracle sees):

```
taint:unverified_web:2      ← unverified web fetch, step 2
taint:tool_flaky:6          ← flaky tool call, step 6
taint:tool_flaky:7          ← flaky tool call, step 7
taint:unverified_web:11     ← unverified web fetch, step 11
taint:stale_cache:16        ← stale cache read, step 16
taint:fallback_model:24     ← fallback model, step 24
```

At step 25 the first compaction fires: lineage truncates to the last K=5
hops, and every taint attached in the folded prefix is dropped — only the
count survives. What `structural_min` still sees:

```
taint:fallback_model:24
```

The oracle blocks the payment (4 gate-relevant taints). `structural_min`
sees zero gate-relevant taints and **proceeds**. Nothing in the surviving
state even hints a check was skipped — the fold counter
(`n_taints_folded`) is the only trace, and a default-allow gate has no
reason to look at it. That single mechanism produces the 3.47% headline rate.

The same state under the other two lineage strategies at this decision
(measured, not hypothetical): **degrade-to-untrusted** sees
`n_taints_folded > 0` and refuses to act (would be a false-stop had the
folded taints been benign — here it happens to be right);
**rehydrate** fetches the 21 folded hops of the value's merged ancestry
from the append-only log (2.6 KB read) and recovers the oracle's block
exactly.

## 2. What the real prose channel actually does (gpt-5-mini)

Same six-taint state pushed through the real summarize→extract round trip
(`--llm openai`). The ≤150-word summary produced:

> Working memory state: freshness 0.700, capability 0.650, tool_integrity
> 0.650, verification 0.450, reconstruction 1.000. Attached taints:
> taint:fallback_model:24, taint:stale_cache:16, taint:tool_flaky:6,
> taint:tool_flaky:7, taint:unverified_web:11, taint:unverified_web:2.
> Overall, the memory is moderately fresh and moderately capable with
> intact reconstruction, but tool integrity is degraded and verification
> is low, indicating reduced trust in outputs that relied on tools or
> external content. Multiple taints note use of a fallback model, stale
> cache data, two instances of flaky tools, and two unverified web
> sources; these should be considered when using or revalidating this state.

Extraction recovered **all five scores exactly and all six taint ids**
(6/6 kept, 0 fabricated). This is why the real channel's realized
parameters beat the mock's assumptions so badly — a modern small model
copies explicit values through prose nearly losslessly.

And yet the same prose arm posts **43% flips on allowlist gates** in the
real-LLM run: the summary preserves *values* but destroys *structure*.
"No fallback hop within the last 8 hops" is a proof about an ordered
window; no amount of faithful prose reconstitutes it. The failure mode of
prose isn't noise — it's the loss of provability.

## 3. Mock channel vs real channel (gpt-5-mini), realized

| parameter | mock (assumed) | real LLM (measured, 993 extractions) |
|---|---|---|
| score noise | gaussian σ = 0.08 | ≈ 0.0001 MAE (near-lossless) |
| taint recall | 0.60 | 0.893 |
| taint precision | 0.90 | 0.904 (245 fabricated taints) |
| parse failures | 0 (configured) | 0 observed |
| flip rate, score gates | 2.4% | 0.08% |
| flip rate, reconstruction gates | 5.0% | 0.0% |
| flip rate, allowlist gates | 29.4% | 43.1% |

Caveat for the post: the summarize prompt hands the model a clean
structured list and asks it to preserve it — a best case for the channel.
Real agent memory interleaves provenance with content; treat the mock's
0.6 recall as a pessimistic bound and gpt-5-mini's 0.89 as an optimistic
one. Both bounds tell the same story on structure-dependent gates.

## 4. Cost of the real-LLM run (estimate)

~993 extractions × 2 calls ≈ 2,000 requests to `gpt-5-mini`;
roughly 0.7M input + 0.5M output tokens ≈ **$1–1.5** at $0.25/$2.00 per
MTok, ~19 minutes wall clock with 8 concurrent round trips per compaction.
Measuring your own memory pipeline this way costs less than a coffee.
