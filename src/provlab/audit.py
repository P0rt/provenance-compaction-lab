"""`prov-lab audit` — static compaction/gate mismatch checker.

The design rule from the series, turned into a linter: *which fields does
your compaction preserve, relative to which fields your gates read?* No
simulation, no traces — pure schema analysis of a small YAML file.

A gate is **starved** when it reads a field the compaction drops, requires a
hop window longer than the kept lineage (W > K), or requires an ordering the
compaction destroys. The gate's polarity then predicts the failure direction:

* ``allow`` (default-allow) + starved → **false-proceed** (fails dangerous)
* ``deny``  (default-deny)  + starved → **false-stop**    (fails expensive)

``irreversible`` ∧ false-proceed is flagged CRITICAL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: the only fields a gate may declare it reads — deliberately not a policy language
READ_FIELDS: tuple[str, ...] = ("scores", "taint_ids", "hop_window", "ordering")
POLARITIES: tuple[str, ...] = ("allow", "deny")

FALSE_PROCEED = "false-proceed"
FALSE_STOP = "false-stop"


class AuditSpecError(ValueError):
    """The audit YAML is malformed."""


@dataclass(frozen=True)
class CompactionSpec:
    """What the compaction preserves."""

    score_aggregates: bool
    taint_ids: bool  # False = only counts survive
    lineage_window: int  # K: hops kept
    ordering_preserved: bool


@dataclass(frozen=True)
class GateSpec:
    name: str
    reads: tuple[str, ...]
    polarity: str  # "allow" | "deny"
    irreversible: bool
    window: int | None = None  # W: required only when reads includes hop_window


@dataclass(frozen=True)
class Finding:
    gate: str
    starved: bool
    reasons: tuple[str, ...]
    direction: str | None  # FALSE_PROCEED | FALSE_STOP | None
    critical: bool


def _require_bool(section: dict[str, Any], key: str, where: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise AuditSpecError(f"{where}: '{key}' must be a boolean, got {value!r}")
    return value


def load_audit_spec(path: Path) -> tuple[CompactionSpec, list[GateSpec]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "compaction" not in raw or "gates" not in raw:
        raise AuditSpecError(
            f"{path}: expected a mapping with 'compaction:' and 'gates:' sections"
        )
    comp_raw = raw["compaction"]
    if not isinstance(comp_raw, dict):
        raise AuditSpecError(f"{path}: 'compaction' must be a mapping")
    window_raw = comp_raw.get("lineage_window")
    if not isinstance(window_raw, int) or isinstance(window_raw, bool) or window_raw < 0:
        raise AuditSpecError(
            f"compaction: 'lineage_window' must be a non-negative integer (K)"
        )
    compaction = CompactionSpec(
        score_aggregates=_require_bool(comp_raw, "score_aggregates", "compaction"),
        taint_ids=_require_bool(comp_raw, "taint_ids", "compaction"),
        lineage_window=window_raw,
        ordering_preserved=_require_bool(comp_raw, "ordering_preserved", "compaction"),
    )
    gates_raw = raw["gates"]
    if not isinstance(gates_raw, list) or not gates_raw:
        raise AuditSpecError(f"{path}: 'gates' must be a non-empty list")
    gates: list[GateSpec] = []
    for i, g in enumerate(gates_raw):
        where = f"gates[{i}]"
        if not isinstance(g, dict):
            raise AuditSpecError(f"{where}: must be a mapping")
        name = g.get("name")
        if not isinstance(name, str) or not name:
            raise AuditSpecError(f"{where}: 'name' must be a non-empty string")
        reads_raw = g.get("reads")
        if not isinstance(reads_raw, list) or not reads_raw:
            raise AuditSpecError(f"{where} ({name}): 'reads' must be a non-empty list")
        reads = tuple(str(r) for r in reads_raw)
        for r in reads:
            if r not in READ_FIELDS:
                raise AuditSpecError(
                    f"{where} ({name}): unknown read field {r!r}; "
                    f"allowed: {', '.join(READ_FIELDS)}"
                )
        polarity = g.get("polarity")
        if polarity not in POLARITIES:
            raise AuditSpecError(
                f"{where} ({name}): 'polarity' must be one of {POLARITIES}"
            )
        window = g.get("window")
        if "hop_window" in reads:
            if not isinstance(window, int) or isinstance(window, bool) or window < 1:
                raise AuditSpecError(
                    f"{where} ({name}): reads hop_window, so 'window' (W) must be "
                    f"a positive integer"
                )
        elif window is not None:
            raise AuditSpecError(
                f"{where} ({name}): 'window' only makes sense with reads: [hop_window]"
            )
        gates.append(
            GateSpec(
                name=name,
                reads=reads,
                polarity=str(polarity),
                irreversible=_require_bool(g, "irreversible", f"{where} ({name})"),
                window=window if isinstance(window, int) else None,
            )
        )
    return compaction, gates


def audit_gate(compaction: CompactionSpec, gate: GateSpec) -> Finding:
    reasons: list[str] = []
    if "scores" in gate.reads and not compaction.score_aggregates:
        reasons.append("reads scores, but compaction drops score aggregates")
    if "taint_ids" in gate.reads and not compaction.taint_ids:
        reasons.append("reads taint ids, but compaction keeps only counts")
    if "hop_window" in gate.reads and gate.window is not None:
        if gate.window > compaction.lineage_window:
            reasons.append(
                f"requires a hop window of W={gate.window}, but compaction keeps "
                f"only K={compaction.lineage_window} hops"
            )
    if "ordering" in gate.reads and not compaction.ordering_preserved:
        reasons.append("requires hop ordering, which compaction destroys")
    starved = bool(reasons)
    direction = None
    if starved:
        direction = FALSE_PROCEED if gate.polarity == "allow" else FALSE_STOP
    critical = starved and gate.irreversible and direction == FALSE_PROCEED
    return Finding(
        gate=gate.name,
        starved=starved,
        reasons=tuple(reasons),
        direction=direction,
        critical=critical,
    )


def run_audit(compaction: CompactionSpec, gates: list[GateSpec]) -> list[Finding]:
    return [audit_gate(compaction, gate) for gate in gates]


def render_findings(
    compaction: CompactionSpec, findings: list[Finding]
) -> str:
    lines = [
        "# Compaction/gate audit",
        "",
        f"Compaction preserves: score aggregates: "
        f"{'yes' if compaction.score_aggregates else 'NO'} · taint ids: "
        f"{'yes' if compaction.taint_ids else 'NO (counts only)'} · lineage "
        f"window K={compaction.lineage_window} · ordering: "
        f"{'preserved' if compaction.ordering_preserved else 'DESTROYED'}",
        "",
        "| gate | starved? | predicted failure | severity | why |",
        "|---|---|---|---|---|",
    ]
    for f in findings:
        severity = "**CRITICAL**" if f.critical else ("warn" if f.starved else "ok")
        lines.append(
            f"| {f.gate} | {'YES' if f.starved else 'no'} "
            f"| {f.direction or '—'} | {severity} "
            f"| {'; '.join(f.reasons) or '—'} |"
        )
    n_critical = sum(f.critical for f in findings)
    n_starved = sum(f.starved for f in findings)
    lines.append("")
    if n_critical:
        lines.append(
            f"**{n_critical} CRITICAL**: irreversible gate(s) predicted to "
            f"false-proceed — they will act on values whose disqualifying "
            f"history the compaction has already erased."
        )
    elif n_starved:
        lines.append(
            f"{n_starved} gate(s) starved, none critical — expect expensive "
            f"false-stops rather than dangerous false-proceeds."
        )
    else:
        lines.append(
            "No starvation: every field your gates read survives your compaction."
        )
    return "\n".join(lines)
