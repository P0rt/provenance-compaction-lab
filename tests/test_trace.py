from __future__ import annotations

from pathlib import Path

import pytest

from provlab.policies import default_policies
from provlab.replay import ReplayConfig, run_replay
from provlab.trace import (
    DEFAULT_TAINT_RULES,
    GenericJsonlAdapter,
    TaintRule,
    TraceError,
    TraceRecord,
    load_taint_rules,
    trace_to_events,
)
from provlab.trajectory import DecisionPoint, MergeEvent, NewValue, OpEvent
from conftest import MED, make_channel

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "traces"


def rec(op: str, step: str = "s1", **fields: object) -> TraceRecord:
    merge_with = fields.pop("merge_with", None)
    return TraceRecord(
        step=step,
        op=op,
        fields=dict(fields),
        merge_with=tuple(str(s) for s in merge_with) if isinstance(merge_with, (list, tuple)) else None,
    )


def test_rule_matching_operators() -> None:
    flaky = TaintRule("tool_flaky", "TOOL_CALL", "not_equals", "status", "ok",
                      "tool_integrity", 0.65)
    assert flaky.matches(rec("TOOL_CALL", status="error"))
    assert not flaky.matches(rec("TOOL_CALL", status="ok"))
    assert not flaky.matches(rec("TOOL_CALL"))  # missing field → no match
    assert not flaky.matches(rec("CACHE_READ", status="error"))  # wrong op
    stale = TaintRule("stale_cache", "CACHE_READ", "gt", "age_seconds", 3600,
                      "freshness", 0.7)
    assert stale.matches(rec("CACHE_READ", age_seconds=7200))
    assert not stale.matches(rec("CACHE_READ", age_seconds=60))
    assert not stale.matches(rec("CACHE_READ", age_seconds="soon"))  # non-numeric
    always = TaintRule("stale_cache", "CACHE_READ", "always", None, None,
                       "freshness", 0.95)
    assert always.matches(rec("CACHE_READ"))


def test_trace_to_events_topology_and_coverage() -> None:
    records = [
        rec("SOURCE_FETCH", step="a", origin="unauthenticated_web"),
        rec("TOOL_CALL", step="b", status="error"),
        rec("UNKNOWN_OP", step="c"),
        rec("SOURCE_FETCH", step="d", origin="internal_db"),  # "a" becomes a recent
        rec("MERGE", step="e", merge_with=["a"]),
        rec("CACHE_READ", step="f", age_seconds=9999),
    ]
    events, coverage = trace_to_events(records, list(DEFAULT_TAINT_RULES), 5)
    assert coverage.n_records == 6
    assert coverage.n_mapped == 5
    assert coverage.skipped == {"unknown op 'UNKNOWN_OP'": 1}
    assert coverage.rule_hits["unverified_web@SOURCE_FETCH"] == 1
    assert coverage.rule_hits["tool_flaky@TOOL_CALL"] == 1
    assert coverage.rule_hits["stale_cache@CACHE_READ"] == 1
    fetch = next(e for e in events if isinstance(e, NewValue) and e.step > 0)
    assert fetch.taint == "taint:unverified_web:1"
    merge = next(e for e in events if isinstance(e, MergeEvent))
    assert merge.step == 4  # renumbered sequentially, skipping the unknown op
    assert len(merge.input_ids) == 2  # the working value + the "a" value
    tool = next(e for e in events if isinstance(e, OpEvent) and e.op == "TOOL_CALL")
    assert tool.taint == "taint:tool_flaky:2"
    assert tool.axis == "tool_integrity"


def test_merge_with_dead_ref_warns_and_degrades() -> None:
    records = [rec("MERGE", step="m", merge_with=["ghost"])]
    events, coverage = trace_to_events(records, [], 5)
    assert coverage.warnings == {"merge_with ref 'ghost' not live": 1}
    assert not any(isinstance(e, MergeEvent) for e in events)
    assert any(isinstance(e, OpEvent) and e.op == "MERGE" for e in events)


def test_decision_points_every_n_mapped_steps() -> None:
    records = [rec("TOOL_CALL", step=str(i), status="ok") for i in range(10)]
    events, _ = trace_to_events(records, [], 5)
    decisions = [e for e in events if isinstance(e, DecisionPoint)]
    assert [d.step for d in decisions] == [5, 10]


def test_sample_trace_replays_deterministically() -> None:
    adapter = GenericJsonlAdapter(EXAMPLES / "sample.jsonl")
    rules = load_taint_rules(EXAMPLES / "rules.yaml")

    def run_once_on_trace() -> str:
        events, coverage = trace_to_events(adapter.records(), rules, 5)
        config = ReplayConfig(
            seed=0, steps=coverage.n_mapped, decision_every=5,
            compaction_cadence=10, keep_hops=5, reconstruction_penalty=0.02,
            profile=MED, rehydrate=True, hop_log_path=None,
        )
        result = run_replay(config, default_policies(8), make_channel(0), events=events)
        return result.decision_log_sha256

    assert run_once_on_trace() == run_once_on_trace()


def test_sample_trace_exercises_every_rule() -> None:
    adapter = GenericJsonlAdapter(EXAMPLES / "sample.jsonl")
    rules = load_taint_rules(EXAMPLES / "rules.yaml")
    _, coverage = trace_to_events(adapter.records(), rules, 5)
    assert all(hits > 0 for hits in coverage.rule_hits.values())
    assert coverage.n_records == 60
    assert sum(coverage.skipped.values()) == 1  # the HUMAN_REVIEW record


def test_rules_yaml_validation(tmp_path: Path) -> None:
    def check(content: str, fragment: str) -> None:
        p = tmp_path / "rules.yaml"
        p.write_text(content)
        with pytest.raises(TraceError, match=fragment):
            load_taint_rules(p)

    check("nope: []", "rules")
    check("rules: [{taint: t, op: NOPE, when: {always: true}, axis: freshness, factor: 0.5}]",
          "'op'")
    check("rules: [{taint: t, op: TOOL_CALL, when: {always: true}, axis: bogus, factor: 0.5}]",
          "'axis'")
    check("rules: [{taint: t, op: TOOL_CALL, when: {field: x}, axis: freshness, factor: 0.5}]",
          "exactly one")
    check("rules: [{taint: '', op: TOOL_CALL, when: {always: true}, axis: freshness, factor: 0.5}]",
          "taint")


def test_jsonl_adapter_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text('{"op": "TOOL_CALL"}\nnot json\n')
    with pytest.raises(TraceError, match="invalid JSON"):
        list(GenericJsonlAdapter(p).records())
