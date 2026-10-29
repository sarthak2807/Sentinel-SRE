"""The contracts every stage of the pipeline speaks.

Keeping these in one place is what lets the detector, the models, the executor
and the dashboard evolve independently - and it is what makes the LLM output
testable, because `Diagnosis` is validated, not trusted.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class TelemetryEvent(BaseModel):
    """One line of telemetry. Logs, metrics and traces all normalise to this."""

    ts: float = Field(default_factory=_now)
    service: str = "sample-service"
    kind: Literal["log", "metric", "trace"] = "log"
    name: str = "http_request"
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str = ""
    value: float | None = None
    route: str | None = None
    status: int | None = None
    duration_ms: float | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class Signal(BaseModel):
    """A single statistical finding that contributed to an anomaly."""

    name: str
    observed: float
    baseline: float
    zscore: float
    unit: str = ""
    note: str = ""


class Anomaly(BaseModel):
    id: str = Field(default_factory=lambda: _id("anom"))
    ts: float = Field(default_factory=_now)
    service: str
    title: str
    signals: list[Signal]
    evidence_logs: list[str] = Field(default_factory=list)
    window_seconds: int = 30

    def fingerprint(self) -> str:
        """Stable key for dedupe and for memory lookups."""
        return f"{self.service}:" + ",".join(sorted(s.name for s in self.signals))


class Triage(BaseModel):
    """Cheap first pass from Nemotron Nano."""

    is_real: bool = True
    severity: Severity = Severity.warning
    category: str = "unknown"
    one_liner: str = ""
    needs_deep_analysis: bool = True
    confidence: float = 0.5

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class ProposedFix(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    expected_effect: str = ""
    rollback: str = ""
    risk: Literal["low", "medium", "high"] = "medium"


class Diagnosis(BaseModel):
    """Structured output from the deep reasoning pass."""

    summary: str
    root_cause: str
    reasoning: str = ""
    confidence: float = 0.5
    blast_radius: str = ""
    similar_incidents: list[str] = Field(default_factory=list)
    proposed_fix: ProposedFix | None = None

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class ExecutionResult(BaseModel):
    ok: bool
    action: str
    where: str = "local"
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    verified: bool | None = None
    verification_note: str = ""


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: _id("inc"))
    opened_at: float = Field(default_factory=_now)
    closed_at: float | None = None
    service: str = "sample-service"
    title: str = ""
    status: Literal["open", "diagnosing", "awaiting_approval", "remediating", "resolved", "dismissed", "monitoring"] = "open"
    severity: Severity = Severity.warning
    # Read-only sources (e.g. a real AWS account) are observed, not acted on:
    # Sentinel diagnoses them but never executes a remediation against them.
    read_only: bool = False
    anomaly: Anomaly | None = None
    triage: Triage | None = None
    diagnosis: Diagnosis | None = None
    execution: ExecutionResult | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    postmortem: str | None = None
    postmortem_path: str | None = None
    # Escalation hand-off for findings Sentinel won't act on (read-only sources).
    ticket: Ticket | None = None

    def log(self, stage: str, detail: str, **extra: Any) -> None:
        self.timeline.append({"ts": _now(), "stage": stage, "detail": detail, **extra})


class MemoryHit(BaseModel):
    incident_id: str
    title: str
    score: float
    root_cause: str = ""
    resolution: str = ""
    occurred_at: str = ""


class Ticket(BaseModel):
    """A hand-off artifact for incidents Sentinel diagnoses but does not act on.

    Read-only sources (a real AWS account) are observed, not remediated, so the
    agent's job ends at a well-formed escalation: an ID, a priority, and a
    markdown body a human can act on. A real Jira / ServiceNow / GitHub backend
    drops in behind the same shape later; `backend` records which one filed it.
    """

    id: str
    created_at: float = Field(default_factory=_now)
    status: str = "open"
    priority: str = "P3"
    assignee: str = "on-call"
    summary: str = ""
    body: str = ""
    path: str | None = None
    backend: str = "local"
