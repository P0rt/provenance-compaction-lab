# Frozen reference data

`llm_gate_metrics.csv` — gate metrics from the real-LLM run (gpt-5-mini prose
channel, C ∈ {10, 25}, med profile, seeds {0, 1}; see `../summary-llm-frozen.md`).
`prov-lab report` falls back to this file for the matched-slice comparison when
a live `results-llm/` directory is not present.
`sweep_metrics.csv` / `sweep_meta.json` — seed-aggregated cadence × penalty sweep (frozen; regenerate with `prov-lab sweep`). Report falls back to these for the crossover analytics.
