"""Run the harness on real agent logs instead of synthetic trajectories.

The generic JSONL trace schema — the contract for real traces — is one JSON
object per line:

    {"step": 17, "op": "TOOL_CALL", "ts": "2026-07-03T10:00:00Z",
     "fields": {"status": "error", "tool": "search"}}

* ``step``      — any identifier; records are replayed in file order and
                  renumbered sequentially (the original id is kept for
                  coverage reporting only).
* ``op``        — one of SOURCE_FETCH, CACHE_READ, LLM_TRANSFORM, TOOL_CALL,
                  MERGE. Records with any other op are skipped and counted.
* ``ts``        — optional, informational.
* ``fields``    — the observable raw fields taint rules match against.
* ``merge_with``— MERGE only, optional: the ``step`` ids of earlier
                  SOURCE_FETCH/MERGE records whose values to merge in.
                  Defaults to the most recent replaced value.

Taint-derivation rules are data, not code — a YAML list mapping observable
fields to taints (and the axis hit that comes with them):

    rules:
      - taint: tool_flaky
        op: TOOL_CALL
        when: {field: status, not_equals: ok}
        axis: tool_integrity
        factor: 0.65

Condition operators: ``equals``, ``not_equals``, ``gt``, ``lt``, ``always``.
The first matching rule wins for a record; a record matching no rule becomes
a clean lineage hop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

import yaml

from .axes import BASE_AXES
from .trajectory import (
    RECENTS_MAX,
    DecisionPoint,
    Event,
    MergeEvent,
    NewValue,
    OpEvent,
    Retire,
)

KNOWN_OPS: tuple[str, ...] = (
    "SOURCE_FETCH",
    "CACHE_READ",
    "LLM_TRANSFORM",
    "TOOL_CALL",
    "MERGE",
)

CONDITION_OPS: tuple[str, ...] = ("equals", "not_equals", "gt", "lt", "always")


class TraceError(ValueError):
    """The trace file or the taint rules are malformed."""


@dataclass(frozen=True)
class TraceRecord:
    """One line of the generic JSONL trace schema."""

    step: str  # the original id, kept verbatim for coverage reporting
    op: str
    fields: dict[str, Any]
    merge_with: tuple[str, ...] | None = None
    ts: str | None = None


class TraceAdapter(Protocol):
    """Anything that yields TraceRecords in replay order."""

    def records(self) -> Iterable[TraceRecord]: ...


class GenericJsonlAdapter:
    """Adapter for the generic JSONL schema documented above."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> Iterable[TraceRecord]:
        for line_no, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as err:
                raise TraceError(f"{self.path}:{line_no}: invalid JSON: {err}") from err
            if not isinstance(obj, dict) or "op" not in obj:
                raise TraceError(f"{self.path}:{line_no}: expected an object with 'op'")
            merge_with_raw = obj.get("merge_with")
            merge_with: tuple[str, ...] | None = None
            if merge_with_raw is not None:
                if not isinstance(merge_with_raw, list):
                    raise TraceError(f"{self.path}:{line_no}: merge_with must be a list")
                merge_with = tuple(str(s) for s in merge_with_raw)
            fields_raw = obj.get("fields", {})
            yield TraceRecord(
                step=str(obj.get("step", line_no)),
                op=str(obj["op"]),
                fields=dict(fields_raw) if isinstance(fields_raw, dict) else {},
                merge_with=merge_with,
                ts=str(obj["ts"]) if "ts" in obj else None,
            )


@dataclass(frozen=True)
class TaintRule:
    taint: str  # taint family, e.g. "tool_flaky"
    op: str  # which op the rule applies to
    cond_op: str  # equals | not_equals | gt | lt | always
    cond_field: str | None
    cond_value: Any
    axis: str
    factor: float

    @property
    def label(self) -> str:
        return f"{self.taint}@{self.op}"

    def matches(self, record: TraceRecord) -> bool:
        if record.op != self.op:
            return False
        if self.cond_op == "always":
            return True
        assert self.cond_field is not None
        if self.cond_field not in record.fields:
            return False
        value = record.fields[self.cond_field]
        if self.cond_op == "equals":
            return bool(value == self.cond_value)
        if self.cond_op == "not_equals":
            return bool(value != self.cond_value)
        try:
            numeric = float(value)
            target = float(self.cond_value)
        except (TypeError, ValueError):
            return False
        return numeric > target if self.cond_op == "gt" else numeric < target


#: the default derivation rules; also shipped as examples/traces/rules.yaml
DEFAULT_TAINT_RULES: tuple[TaintRule, ...] = (
    TaintRule("unverified_web", "SOURCE_FETCH", "equals", "origin",
              "unauthenticated_web", "verification", 0.45),
    TaintRule("stale_cache", "CACHE_READ", "gt", "age_seconds",
              3600, "freshness", 0.70),
    TaintRule("fallback_model", "LLM_TRANSFORM", "equals", "model_tier",
              "fallback", "capability", 0.65),
    TaintRule("tool_flaky", "TOOL_CALL", "not_equals", "status",
              "ok", "tool_integrity", 0.65),
)


def load_taint_rules(path: Path) -> list[TaintRule]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        raise TraceError(f"{path}: expected a mapping with a 'rules:' list")
    rules: list[TaintRule] = []
    for i, r in enumerate(raw["rules"]):
        where = f"{path}: rules[{i}]"
        if not isinstance(r, dict):
            raise TraceError(f"{where}: must be a mapping")
        op = str(r.get("op", ""))
        if op not in KNOWN_OPS:
            raise TraceError(f"{where}: 'op' must be one of {KNOWN_OPS}")
        axis = str(r.get("axis", ""))
        if axis not in BASE_AXES:
            raise TraceError(f"{where}: 'axis' must be one of {BASE_AXES}")
        when = r.get("when")
        if not isinstance(when, dict) or not when:
            raise TraceError(f"{where}: 'when' must be a non-empty mapping")
        if "always" in when:
            cond_op, cond_field, cond_value = "always", None, None
        else:
            if "field" not in when:
                raise TraceError(f"{where}: 'when' needs a 'field'")
            ops_present = [k for k in when if k in CONDITION_OPS]
            if len(ops_present) != 1:
                raise TraceError(
                    f"{where}: 'when' needs exactly one of {CONDITION_OPS}"
                )
            cond_op = ops_present[0]
            cond_field = str(when["field"])
            cond_value = when[cond_op]
        try:
            factor = float(r["factor"])
        except (KeyError, TypeError, ValueError) as err:
            raise TraceError(f"{where}: 'factor' must be a float") from err
        taint = str(r.get("taint", ""))
        if not taint:
            raise TraceError(f"{where}: 'taint' must be a non-empty string")
        rules.append(
            TaintRule(taint, op, cond_op, cond_field, cond_value, axis, factor)
        )
    return rules


@dataclass
class TraceCoverage:
    """What the adapter could and could not map — reported, never silent."""

    n_records: int = 0
    n_mapped: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason → count
    #: non-fatal mapping notes (e.g. a merge_with ref that is no longer live);
    #: the record itself still maps
    warnings: dict[str, int] = field(default_factory=dict)
    rule_hits: dict[str, int] = field(default_factory=dict)  # rule label → count

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def warn(self, reason: str) -> None:
        self.warnings[reason] = self.warnings.get(reason, 0) + 1

    def as_json_obj(self) -> dict[str, Any]:
        return {
            "n_records": self.n_records,
            "n_mapped": self.n_mapped,
            "n_skipped": sum(self.skipped.values()),
            "skipped_by_reason": dict(sorted(self.skipped.items())),
            "warnings": dict(sorted(self.warnings.items())),
            "rule_hits": dict(sorted(self.rule_hits.items())),
        }


def trace_to_events(
    records: Iterable[TraceRecord],
    rules: list[TaintRule],
    decision_every: int,
) -> tuple[list[Event], TraceCoverage]:
    """Convert a trace into replay events, mirroring the synthetic generator's
    value topology (working value, recents, retirement). Records are
    renumbered sequentially; the oracle arm is simply the full-provenance
    replay of the same events."""
    coverage = TraceCoverage()
    for known_rule in rules:
        coverage.rule_hits.setdefault(known_rule.label, 0)
    events: list[Event] = [NewValue(step=0, value_id=0, unverified=False)]
    working = 0
    next_id = 1
    recents: list[int] = []
    value_by_trace_step: dict[str, int] = {}
    step = 0

    def first_match(record: TraceRecord) -> TaintRule | None:
        for candidate in rules:
            if candidate.matches(record):
                coverage.rule_hits[candidate.label] = (
                    coverage.rule_hits.get(candidate.label, 0) + 1
                )
                return candidate
        return None

    def replace_working(new_id: int, at_step: int) -> None:
        nonlocal working
        recents.append(working)
        if len(recents) > RECENTS_MAX:
            events.append(Retire(step=at_step, value_id=recents.pop(0)))
        working = new_id

    for record in records:
        coverage.n_records += 1
        if record.op not in KNOWN_OPS:
            coverage.skip(f"unknown op {record.op!r}")
            continue
        coverage.n_mapped += 1
        step += 1
        rule = first_match(record)
        taint = f"taint:{rule.taint}:{step}" if rule is not None else None
        if record.op == "SOURCE_FETCH":
            new_id = next_id
            next_id += 1
            events.append(
                NewValue(
                    step=step,
                    value_id=new_id,
                    unverified=False,
                    axis=rule.axis if rule is not None else None,
                    factor=rule.factor if rule is not None else 1.0,
                    taint=taint,
                )
            )
            value_by_trace_step[record.step] = new_id
            replace_working(new_id, step)
        elif record.op == "MERGE":
            input_ids = [working]
            if record.merge_with:
                for ref in record.merge_with:
                    ref_id = value_by_trace_step.get(ref)
                    if ref_id is None or (ref_id != working and ref_id not in recents):
                        coverage.warn(f"merge_with ref {ref!r} not live")
                        continue
                    if ref_id != working and ref_id not in input_ids:
                        input_ids.append(ref_id)
            elif recents:
                input_ids.append(recents[-1])
            if len(input_ids) < 2:
                events.append(
                    OpEvent(step=step, value_id=working, op="MERGE",
                            axis=None, factor=1.0, taint=None)
                )
                continue
            new_id = next_id
            next_id += 1
            events.append(
                MergeEvent(step=step, value_id=new_id, input_ids=tuple(input_ids))
            )
            value_by_trace_step[record.step] = new_id
            replace_working(new_id, step)
        else:
            events.append(
                OpEvent(
                    step=step,
                    value_id=working,
                    op=record.op,
                    axis=rule.axis if rule is not None else None,
                    factor=rule.factor if rule is not None else 1.0,
                    taint=taint,
                )
            )
        if step % decision_every == 0:
            events.append(DecisionPoint(step=step, value_id=working))
    return events, coverage
