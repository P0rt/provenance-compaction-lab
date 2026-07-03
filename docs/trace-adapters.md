# Running the harness on real agent traces

`prov-lab run --trace path.jsonl [--taint-rules rules.yaml]` replaces the
synthetic generator with your logs. The oracle arm is a full-provenance
replay of the same trace; arms, gates, replay, and report are unchanged. The
report gains a **Trace coverage** block: records mapped vs skipped (with
reasons), and taints derived per rule.

## The generic JSONL schema (the contract)

One JSON object per line, replayed in file order:

```json
{"step": "s017", "op": "TOOL_CALL", "ts": "2026-07-03T10:00:00Z",
 "fields": {"status": "error", "tool": "search"}}
```

| key | required | meaning |
|---|---|---|
| `op` | ✅ | one of `SOURCE_FETCH`, `CACHE_READ`, `LLM_TRANSFORM`, `TOOL_CALL`, `MERGE`. Anything else is skipped and counted. |
| `step` | — | any identifier; kept verbatim for coverage and `merge_with` references. Records are renumbered sequentially for replay. |
| `ts` | — | informational timestamp |
| `fields` | — | the observable raw fields taint rules match against |
| `merge_with` | — | MERGE only: `step` ids of earlier SOURCE_FETCH/MERGE records whose values to merge in (defaults to the most recent replaced value) |

## Taint-derivation rules (data, not code)

```yaml
rules:
  - taint: tool_flaky
    op: TOOL_CALL
    when: {field: status, not_equals: ok}
    axis: tool_integrity
    factor: 0.65
```

Condition operators: `equals`, `not_equals`, `gt`, `lt`, `always`. The first
matching rule wins; a record matching no rule becomes a clean lineage hop.
Defaults (shipped as `examples/traces/rules.yaml`): tool result status ≠ ok →
`taint:tool_flaky`; cache age > 3600 s → `taint:stale_cache`; response
metadata marking a fallback model → `taint:fallback_model`; content origin =
unauthenticated web → `taint:unverified_web`.

Try it:

```sh
uv run prov-lab run --trace examples/traces/sample.jsonl --mock --out results-trace
uv run prov-lab report --out results-trace
```

## Recipe: mapping Claude Code session transcripts

We deliberately do **not** ship a Claude Code adapter: the local transcript
format (`~/.claude/projects/<project>/<session>.jsonl`) nests tool activity
inside message content blocks and drifts across versions — a checked-in
adapter would rot. The mapping is mechanical enough to write against your
own transcript version in ~50 lines; the contract you're converting *to* is
the schema above.

| transcript signal | generic op | suggested `fields` |
|---|---|---|
| assistant message containing a `tool_use` block for WebFetch / WebSearch | `SOURCE_FETCH` | `origin`: `unauthenticated_web` unless the URL's domain is on your allowlist |
| `tool_use` for Read / Glob / Grep on cached artifacts | `CACHE_READ` | `age_seconds`: now − file mtime |
| every assistant turn (the model transformed the working state) | `LLM_TRANSFORM` | `model_tier`: `fallback` when the `model` field differs from your session's primary model |
| any other `tool_use` + its matching `tool_result` | `TOOL_CALL` | `status`: `error` when the tool_result carries `is_error: true`, else `ok` |
| resuming with `--continue` / compaction summaries | `MERGE` | `merge_with`: the step ids of the sessions/values being folded in |

Notes from writing this recipe against real transcripts:

* Pair each `tool_use` with its `tool_result` by `tool_use_id` — they arrive
  in different records.
* Sub-agent (Task tool) activity is its own nested trajectory; either flatten
  it into `TOOL_CALL`s or replay it as a separate trace and MERGE the result.
* Anything you cannot map, leave with its original `op` name — the coverage
  block will count it as skipped instead of silently dropping it, and that
  number tells you how much of the session the analysis actually saw.
