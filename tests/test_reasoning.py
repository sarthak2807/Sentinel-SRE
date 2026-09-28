"""Model output is untrusted input. These tests cover the parsing and routing."""
import pytest

from sentinel.models import (
    Anomaly,
    Diagnosis,
    ExecutionResult,
    Incident,
    MemoryHit,
    ProposedFix,
    Severity,
    Signal,
    Triage,
)
from sentinel.nemotron import NemotronClient
from sentinel.reasoning import (
    _coerce_diagnosis,
    _coerce_triage,
    _offline_postmortem,
    _postmortem_context,
    pick_deep_tier,
)


class _Message:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


def test_reads_reasoning_content_when_content_is_empty():
    """Nemotron reasoning models often leave `content` blank. This is the trap."""
    assert NemotronClient._extract(_Message(content="", reasoning_content="the answer")) == "the answer"
    assert NemotronClient._extract(_Message(content="direct", reasoning_content="ignored")) == "direct"


def test_json_is_recovered_from_prose_and_fences():
    text = 'Let me think about this.\n```json\n{"is_real": true, "severity": "critical"}\n```\nDone.'
    parsed = NemotronClient._first_json_object(text)
    assert parsed == {"is_real": True, "severity": "critical"}


def test_json_recovery_handles_braces_inside_strings():
    text = '{"one_liner": "latency {p95} tripled", "confidence": 0.9}'
    parsed = NemotronClient._first_json_object(text)
    assert parsed["one_liner"] == "latency {p95} tripled"


def test_bad_severity_is_coerced_not_crashed():
    triage = Triage(**_coerce_triage({"severity": "CATASTROPHIC", "confidence": 9.9}))
    assert triage.severity == Severity.warning
    assert triage.confidence == 1.0


def test_diagnosis_survives_a_malformed_fix():
    diagnosis = _coerce_diagnosis(
        {"summary": "s", "root_cause": "r", "proposed_fix": {"action": "restart_service", "params": "oops"}}
    )
    assert diagnosis.proposed_fix.params == {}
    assert diagnosis.proposed_fix.risk == "medium"


def _anomaly(n_signals: int) -> Anomaly:
    return Anomaly(
        service="s",
        title="t",
        signals=[Signal(name=f"m{i}", observed=1, baseline=0.1, zscore=5) for i in range(n_signals)],
    )


def test_escalation_reserves_ultra_for_hard_cases():
    cheap = Triage(severity=Severity.warning, category="memory_leak", confidence=0.9)
    hit = [MemoryHit(incident_id="x", title="t", score=0.8)]
    assert pick_deep_tier(_anomaly(1), cheap, hit) == "super"

    uncertain = Triage(severity=Severity.critical, category="unknown", confidence=0.3)
    assert pick_deep_tier(_anomaly(1), uncertain, hit) == "ultra"
    assert pick_deep_tier(_anomaly(3), cheap, hit) == "ultra"
    assert pick_deep_tier(_anomaly(1), Triage(category="unknown", confidence=0.9), []) == "ultra"


def _resolved_incident() -> Incident:
    inc = Incident(
        title="Memory climbing on order service",
        severity=Severity.critical,
        anomaly=Anomaly(
            service="sample-service",
            title="Memory climbing on order service",
            signals=[Signal(name="rss_mb", observed=1400, baseline=120, zscore=8, unit="MB")],
        ),
        triage=Triage(category="memory_leak", one_liner="RSS growing unbounded", confidence=0.8),
        diagnosis=Diagnosis(
            summary="Unbounded cache retained every payload.",
            root_cause="A response cache keyed on the full request never evicted.",
            confidence=0.82,
            blast_radius="order workers, eventual OOM",
            proposed_fix=ProposedFix(
                action="restart_service",
                rationale="reclaim leaked heap while a bounded cache ships",
                rollback="none needed",
            ),
        ),
        execution=ExecutionResult(ok=True, action="restart_service", where="sandbox", verified=True,
                                  verification_note="rss_mb back to 130MB"),
    )
    inc.closed_at = inc.opened_at + 185
    inc.log("detect", "Memory climbing on order service")
    inc.log("diagnose", "Unbounded cache retained every payload.")
    return inc


def test_postmortem_context_flattens_a_resolved_incident():
    ctx = _postmortem_context(_resolved_incident())
    assert ctx["fix_action"] == "restart_service"
    assert ctx["duration"] == "3m 5s"
    assert "cache" in ctx["root_cause"]
    assert ctx["confidence"] == "82%"


def test_offline_postmortem_is_well_formed_markdown():
    md = _offline_postmortem(_postmortem_context(_resolved_incident()))
    for section in ("# Postmortem:", "## Impact", "## Timeline", "## Root cause",
                    "## Resolution", "## Prevention"):
        assert section in md
    assert "restart_service" in md
    assert "response cache" in md or "cache" in md
