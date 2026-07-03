from __future__ import annotations

from pathlib import Path

import pytest

from provlab.audit import (
    FALSE_PROCEED,
    FALSE_STOP,
    AuditSpecError,
    CompactionSpec,
    GateSpec,
    audit_gate,
    load_audit_spec,
    render_findings,
    run_audit,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "audit"

LOSSY = CompactionSpec(
    score_aggregates=True, taint_ids=False, lineage_window=5, ordering_preserved=True
)


def gate(**kwargs: object) -> GateSpec:
    defaults: dict[str, object] = {
        "name": "g", "reads": ("taint_ids",), "polarity": "allow",
        "irreversible": False, "window": None,
    }
    defaults.update(kwargs)
    return GateSpec(**defaults)  # type: ignore[arg-type]


def test_dangerous_example_flags_payment_as_critical_false_proceed() -> None:
    compaction, gates = load_audit_spec(EXAMPLES / "dangerous.yaml")
    findings = {f.gate: f for f in run_audit(compaction, gates)}
    payment = findings["payment_no_untrusted_taint"]
    assert payment.starved and payment.critical
    assert payment.direction == FALSE_PROCEED
    window = findings["audit_requires_clean_window"]
    assert window.starved and not window.critical
    assert window.direction == FALSE_STOP
    assert not findings["summarize_freshness_strict"].starved
    rendered = render_findings(compaction, run_audit(compaction, gates))
    assert "CRITICAL" in rendered


def test_healthy_example_has_no_starvation() -> None:
    compaction, gates = load_audit_spec(EXAMPLES / "healthy.yaml")
    findings = run_audit(compaction, gates)
    assert not any(f.starved for f in findings)
    assert "No starvation" in render_findings(compaction, findings)


def test_hop_window_starves_only_when_w_exceeds_k() -> None:
    ok = audit_gate(LOSSY, gate(reads=("hop_window",), window=5, polarity="deny"))
    assert not ok.starved
    starved = audit_gate(LOSSY, gate(reads=("hop_window",), window=6, polarity="deny"))
    assert starved.starved and starved.direction == FALSE_STOP


def test_ordering_and_scores_reads() -> None:
    no_order = CompactionSpec(
        score_aggregates=True, taint_ids=True, lineage_window=5,
        ordering_preserved=False,
    )
    f = audit_gate(no_order, gate(reads=("ordering",), polarity="allow"))
    assert f.starved and f.direction == FALSE_PROCEED
    no_scores = CompactionSpec(
        score_aggregates=False, taint_ids=True, lineage_window=5,
        ordering_preserved=True,
    )
    f2 = audit_gate(no_scores, gate(reads=("scores",), polarity="deny"))
    assert f2.starved and f2.direction == FALSE_STOP


def test_deny_polarity_is_never_critical() -> None:
    f = audit_gate(LOSSY, gate(polarity="deny", irreversible=True))
    assert f.starved and f.direction == FALSE_STOP and not f.critical


def test_spec_validation_errors(tmp_path: Path) -> None:
    def check(content: str, fragment: str) -> None:
        p = tmp_path / "spec.yaml"
        p.write_text(content)
        with pytest.raises(AuditSpecError, match=fragment):
            load_audit_spec(p)

    check("compaction: {}", "gates")
    check(
        "compaction: {score_aggregates: yes, taint_ids: yes, lineage_window: 5,"
        " ordering_preserved: yes}\ngates: []",
        "non-empty list",
    )
    base = (
        "compaction: {score_aggregates: yes, taint_ids: yes, lineage_window: 5,"
        " ordering_preserved: yes}\n"
    )
    check(base + "gates: [{name: g, reads: [magic], polarity: allow, irreversible: no}]",
          "unknown read field")
    check(base + "gates: [{name: g, reads: [scores], polarity: maybe, irreversible: no}]",
          "polarity")
    check(base + "gates: [{name: g, reads: [hop_window], polarity: deny, irreversible: no}]",
          "'window'")
    check(base + "gates: [{name: g, reads: [scores], window: 3, polarity: deny, irreversible: no}]",
          "only makes sense")
