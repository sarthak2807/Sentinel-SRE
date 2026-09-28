"""The reasoning stages: Nano triage, Super/Ultra diagnosis, and incident chat.

The routing here is the whole point of the design. Detection fires far more
often than incidents actually happen, so:

    every anomaly        -> Nemotron Nano   (~fast, cheap, binary-ish decision)
    anomalies Nano keeps -> Super or Ultra  (expensive, long-context, reasoning)

Ultra is reserved for the cases that are genuinely hard: low triage confidence,
several correlated signals at once, or no useful match in incident memory. That
escalation rule lives in `pick_deep_tier` and is one function precisely so it can
be tuned and tested.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .actions import describe_for_prompt
from .models import Anomaly, Diagnosis, Incident, MemoryHit, ProposedFix, Severity, Triage
from .nemotron import client, load_prompt

log = logging.getLogger("sentinel.reasoning")


# ------------------------------------------------------------------ rendering


def render_signals(anomaly: Anomaly) -> str:
    return "\n".join(
        f"- {s.name}: {s.observed}{s.unit} now vs {s.baseline}{s.unit} baseline "
        f"(z={s.zscore}) - {s.note}"
        for s in anomaly.signals
    )


def render_logs(anomaly: Anomaly) -> str:
    return "\n".join(anomaly.evidence_logs) or "(no error-level log lines in the window)"


def render_memory(hits: list[MemoryHit]) -> str:
    if not hits:
        return "(no similar incidents on record)"
    return "\n".join(
        f"- [{h.incident_id}] {h.title} (similarity {h.score}, {h.occurred_at})\n"
        f"    root cause: {h.root_cause}\n"
        f"    resolution: {h.resolution}"
        for h in hits
    )


def render_timeline(incident: Incident) -> str:
    lines = [
        f"- {time.strftime('%H:%M:%S', time.localtime(entry.get('ts', 0)))} "
        f"[{entry.get('stage', '')}] {entry.get('detail', '')}"
        for entry in incident.timeline
    ]
    return "\n".join(lines) or "(no timeline recorded)"


# --------------------------------------------------------------------- triage


async def triage_anomaly(anomaly: Anomaly) -> Triage:
    prompt = load_prompt("triage.txt").format(
        service=anomaly.service,
        detected_at=time.strftime("%H:%M:%S", time.localtime(anomaly.ts)),
        window=anomaly.window_seconds,
        signals=render_signals(anomaly),
        logs=render_logs(anomaly),
    )
    try:
        data, _ = await client.complete_json(
            "nano",
            [
                {"role": "system", "content": "You output JSON only."},
                {"role": "user", "content": prompt},
            ],
            purpose="triage",
            max_tokens=700,
        )
        return Triage(**_coerce_triage(data))
    except Exception as exc:  # noqa: BLE001
        log.warning("Triage failed (%s) - escalating conservatively.", exc)
        return Triage(
            is_real=True,
            severity=Severity.warning,
            category="unknown",
            one_liner="Triage model unavailable; escalating so a human sees it.",
            needs_deep_analysis=True,
            confidence=0.2,
        )


def _coerce_triage(data: dict[str, Any]) -> dict[str, Any]:
    severity = str(data.get("severity", "warning")).lower()
    if severity not in {"info", "warning", "critical"}:
        severity = "warning"
    return {
        "is_real": bool(data.get("is_real", True)),
        "severity": severity,
        "category": str(data.get("category", "unknown")),
        "one_liner": str(data.get("one_liner", ""))[:200],
        "needs_deep_analysis": bool(data.get("needs_deep_analysis", True)),
        "confidence": float(data.get("confidence", 0.5) or 0.5),
    }


# ------------------------------------------------------------------ escalation


def pick_deep_tier(anomaly: Anomaly, triage: Triage, memory_hits: list[MemoryHit]) -> str:
    """Super by default; Ultra only when the problem is genuinely ambiguous."""
    if triage.severity == Severity.critical and triage.confidence < 0.6:
        return "ultra"
    if len(anomaly.signals) >= 3:
        return "ultra"  # correlated signals need real cross-metric reasoning
    if not memory_hits and triage.category in {"unknown", "deploy_regression"}:
        return "ultra"  # nothing to pattern-match against
    return "super"


# ------------------------------------------------------------------ diagnosis


async def diagnose(
    anomaly: Anomaly,
    triage: Triage,
    memory_hits: list[MemoryHit],
    health: dict[str, Any] | None,
    tier: str | None = None,
) -> tuple[Diagnosis, str]:
    tier = tier or pick_deep_tier(anomaly, triage, memory_hits)
    prompt = load_prompt("diagnose.txt").format(
        service=anomaly.service,
        triage_category=triage.category,
        triage_severity=triage.severity.value,
        triage_one_liner=triage.one_liner,
        signals=render_signals(anomaly),
        logs=render_logs(anomaly),
        health=json.dumps(health or {}, indent=2)[:1500],
        memory=render_memory(memory_hits),
        actions=describe_for_prompt(),
    )
    data, _ = await client.complete_json(
        tier,
        [
            {
                "role": "system",
                "content": "You are a precise site-reliability analyst. You output JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        purpose=f"diagnose:{tier}",
        max_tokens=2200,
    )
    return _coerce_diagnosis(data), tier


def _coerce_diagnosis(data: dict[str, Any]) -> Diagnosis:
    fix_data = data.get("proposed_fix") or None
    fix = None
    if isinstance(fix_data, dict) and fix_data.get("action"):
        params = fix_data.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        fix = ProposedFix(
            action=str(fix_data["action"]).strip(),
            params=params,
            rationale=str(fix_data.get("rationale", "")),
            expected_effect=str(fix_data.get("expected_effect", "")),
            rollback=str(fix_data.get("rollback", "")),
            risk=str(fix_data.get("risk", "medium")).lower()
            if str(fix_data.get("risk", "")).lower() in {"low", "medium", "high"}
            else "medium",
        )
    similar = data.get("similar_incidents") or []
    if not isinstance(similar, list):
        similar = []
    return Diagnosis(
        summary=str(data.get("summary", "")).strip() or "No summary returned.",
        root_cause=str(data.get("root_cause", "")).strip() or "Undetermined.",
        reasoning=str(data.get("reasoning", "")).strip(),
        confidence=float(data.get("confidence", 0.5) or 0.5),
        blast_radius=str(data.get("blast_radius", "")).strip(),
        similar_incidents=[str(s) for s in similar][:5],
        proposed_fix=fix,
    )


# ----------------------------------------------------------------- chat turn


async def answer_question(
    question: str, state: dict[str, Any], memory_hits: list[MemoryHit]
) -> str:
    prompt = load_prompt("chat.txt").format(
        state=json.dumps(state, indent=2, default=str)[:9000],
        memory=render_memory(memory_hits),
        question=question,
    )
    text, _ = await client.complete(
        "super",
        [
            {
                "role": "system",
                "content": "You are Sentinel, an on-call SRE copilot. Answer briefly and concretely.",
            },
            {"role": "user", "content": prompt},
        ],
        purpose="chat",
        max_tokens=900,
        temperature=0.3,
    )
    return text.strip()


# ---------------------------------------------------------------- postmortem


def _postmortem_context(incident: Incident) -> dict[str, str]:
    """Flatten a resolved incident into the fields the postmortem prompt reads."""
    d = incident.diagnosis
    fix = d.proposed_fix if d else None
    ex = incident.execution
    opened = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident.opened_at))
    closed = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(incident.closed_at))
        if incident.closed_at
        else "—"
    )
    if incident.closed_at:
        secs = max(0, int(incident.closed_at - incident.opened_at))
        duration = f"{secs // 60}m {secs % 60}s"
    else:
        duration = "—"
    return {
        "incident_id": incident.id,
        "title": incident.title or (incident.anomaly.title if incident.anomaly else "Incident"),
        "service": incident.service,
        "severity": incident.severity.value,
        "opened_at": opened,
        "closed_at": closed,
        "duration": duration,
        "signals": render_signals(incident.anomaly) if incident.anomaly else "(none recorded)",
        "summary": (d.summary if d else "") or "",
        "root_cause": (d.root_cause if d else "") or "Undetermined.",
        "reasoning": (d.reasoning if d else "") or "",
        "blast_radius": (d.blast_radius if d else "") or "not established",
        "confidence": f"{round((d.confidence if d else 0.0) * 100)}%",
        "fix_action": fix.action if fix else "none",
        "fix_rationale": (fix.rationale if fix else "") or "",
        "fix_rollback": (fix.rollback if fix else "") or "n/a",
        "verification": (ex.verification_note if ex else "") or "signals returned to baseline",
        "where": (ex.where if ex else "") or "local",
        "timeline": render_timeline(incident),
    }


def _offline_postmortem(ctx: dict[str, str]) -> str:
    """Deterministic postmortem so the demo produces a real document offline too."""
    summary = ctx["summary"] or (
        f"A {ctx['severity']} incident on {ctx['service']} was detected, diagnosed, "
        f"and remediated by Sentinel."
    )
    return f"""# Postmortem: {ctx['title']}

> Generated by Sentinel's offline stub (no NEBIUS_API_KEY set). Add the key for a model-written narrative.

**Summary** — {summary}

## Impact
{ctx['blast_radius']}. Severity {ctx['severity']}, open from {ctx['opened_at']} to {ctx['closed_at']} ({ctx['duration']}).

## Timeline
{ctx['timeline']}

## Root cause
{ctx['root_cause']}

## Resolution
Applied `{ctx['fix_action']}` — {ctx['fix_rationale']}. Verification: {ctx['verification']} (ran in {ctx['where']}). Rollback: {ctx['fix_rollback']}.

## Prevention
- Add an alert on the signals above so this shape is caught earlier.
- Capture this remediation as a runbook entry for `{ctx['fix_action']}`.
- Review whether the root cause can be removed rather than mitigated.
"""


async def write_postmortem(incident: Incident) -> str:
    """Produce a Markdown postmortem for a resolved incident."""
    ctx = _postmortem_context(incident)
    if not client.online:
        return _offline_postmortem(ctx)
    prompt = load_prompt("postmortem.txt").format(**ctx)
    text, _ = await client.complete(
        "super",
        [
            {
                "role": "system",
                "content": (
                    "You are an SRE writing a concise, blameless postmortem in "
                    "GitHub-flavored Markdown. Output only the Markdown document."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        purpose="postmortem",
        max_tokens=3000,
        temperature=0.3,
    )
    return text.strip() or _offline_postmortem(ctx)
