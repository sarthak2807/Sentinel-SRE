"""The orchestrator: telemetry in, incidents out.

One loop, five stages, every transition published to the bus so the dashboard is
a pure view of agent state rather than a second source of truth.

    ingest -> detect -> triage(Nano) -> recall(RAG) -> diagnose(Super/Ultra)
           -> await approval -> execute(sandbox) -> verify -> remember
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from .bus import bus
from .config import settings
from .detector import AnomalyDetector
from .executor import ApprovalRequired, executor
from .memory import memory
from .models import Anomaly, Incident, Severity, TelemetryEvent
from .nemotron import ledger
from .reasoning import answer_question, diagnose, triage_anomaly, write_postmortem
from .ticketing import desk as ticket_desk

log = logging.getLogger("sentinel.pipeline")


class Sentinel:
    OPEN_STATUSES = {"open", "diagnosing", "awaiting_approval", "remediating", "monitoring"}

    def __init__(self) -> None:
        self.detector = AnomalyDetector()
        self.incidents: dict[str, Incident] = {}
        self.order: list[str] = []
        self._task: asyncio.Task | None = None
        self._in_flight: set[str] = set()
        self.started_at = time.time()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("Sentinel watch loop started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                for anomaly in self.detector.evaluate():
                    asyncio.create_task(self.handle_anomaly(anomaly))
            except Exception:  # noqa: BLE001
                log.exception("Detector tick failed")
            await asyncio.sleep(2)

    # --------------------------------------------------------------- ingest

    async def ingest(self, event: TelemetryEvent) -> None:
        self.detector.observe(event)
        if event.level in {"warn", "error"} or event.kind == "metric":
            await bus.publish("telemetry", event.model_dump(mode="json"))

    # -------------------------------------------------------------- pipeline

    async def handle_anomaly(self, anomaly) -> Incident | None:
        fingerprint = anomaly.fingerprint()
        if fingerprint in self._in_flight:
            return None
        # Don't open a second incident for a fault that's already being
        # worked - a fix sitting at the approval gate is not a new problem,
        # it's the same problem waiting on a human. Refresh its evidence
        # instead of spawning a duplicate card.
        existing = self._open_incident_for(anomaly)
        if existing:
            existing.anomaly = anomaly
            existing.log("detect", f"Still active: {anomaly.title}", signals=[s.model_dump() for s in anomaly.signals])
            await self._emit(existing, "incident.detected")
            return existing
        self._in_flight.add(fingerprint)

        incident = Incident(
            service=anomaly.service, title=anomaly.title, anomaly=anomaly, status="open"
        )
        # Anything other than the local sample service describes infra we only
        # observe (e.g. a real AWS account). Diagnose it, never act on it - the
        # detector now labels each anomaly with its owning service, so this is a
        # reliable signal rather than a guess from the signal names.
        if anomaly.service != "sample-service":
            incident.read_only = True
        self.incidents[incident.id] = incident
        self.order.insert(0, incident.id)
        incident.log("detect", anomaly.title, signals=[s.model_dump() for s in anomaly.signals])
        await self._emit(incident, "incident.detected")

        try:
            # 1. Cheap triage on Nano.
            triage = await triage_anomaly(anomaly)
            incident.triage = triage
            incident.severity = triage.severity
            incident.log("triage", triage.one_liner, category=triage.category,
                         confidence=triage.confidence)

            if not triage.is_real:
                incident.status = "dismissed"
                incident.log("triage", "Classified as noise - no human paged.")
                await self._emit(incident, "incident.dismissed")
                return incident

            incident.status = "diagnosing"
            await self._emit(incident, "incident.triaged")

            # 2. Recall similar past incidents.
            hits = await memory.search_for_anomaly(anomaly)
            incident.log(
                "recall",
                f"Retrieved {len(hits)} similar incident(s) from memory",
                hits=[h.model_dump() for h in hits],
            )
            await self._emit(incident, "incident.recalled")

            # 3. Deep root-cause reasoning on Super or Ultra.
            health = await self._service_health()
            diagnosis, tier = await diagnose(anomaly, triage, hits, health)
            incident.diagnosis = diagnosis
            incident.log("diagnose", diagnosis.summary, tier=tier,
                         confidence=diagnosis.confidence)

            # 4. Hold at the approval gate.
            if incident.read_only:
                # Read-only source: present the diagnosis as an advisory. We do
                # not preview or execute any action against an account we only
                # observe, whatever fix the model may have proposed.
                incident.status = "monitoring"
                incident.log("propose", "Advisory only - read-only source, no remediation executed.")
            elif diagnosis.proposed_fix:
                try:
                    preview = executor.preview(diagnosis.proposed_fix)
                    incident.status = "awaiting_approval"
                    incident.log("propose", f"Proposed {preview['action']}", preview=preview)
                except ValueError as exc:
                    incident.log("propose", f"Rejected the model's fix: {exc}")
                    incident.status = "open"
            else:
                incident.status = "open"

            await self._emit(incident, "incident.diagnosed")

            if settings.auto_approve and incident.status == "awaiting_approval":
                await self.approve(incident.id, approved_by="auto-approve")
            return incident
        except Exception as exc:  # noqa: BLE001
            log.exception("Pipeline failed for %s", incident.id)
            incident.log("error", f"Pipeline error: {exc}")
            await self._emit(incident, "incident.error")
            return incident
        finally:
            self._in_flight.discard(fingerprint)

    async def approve(self, incident_id: str, approved_by: str = "on-call") -> Incident:
        incident = self.incidents[incident_id]
        incident.status = "remediating"
        await self._emit(incident, "incident.remediating")
        try:
            result = await executor.execute(incident, approved_by=approved_by)
        except (ApprovalRequired, ValueError) as exc:
            incident.log("execute", f"Blocked: {exc}")
            incident.status = "awaiting_approval"
            await self._emit(incident, "incident.blocked")
            return incident

        incident.execution = result
        incident.log(
            "verify",
            result.verification_note or ("Fix applied." if result.ok else "Fix failed."),
            ok=result.ok,
            verified=result.verified,
        )
        genuine_fix = incident.diagnosis.proposed_fix.action != "collect_diagnostics"
        resolved = result.ok and result.verified is not False
        if resolved:
            incident.status = "resolved"
            incident.closed_at = time.time()
            # collect_diagnostics means "nothing was actually wrong" - writing
            # that to memory as a resolved incident pollutes retrieval for
            # real incidents later. Only remember genuine fixes.
            if genuine_fix:
                stored = await memory.remember_resolved(incident)
                if stored:
                    incident.log("remember", f"Written to incident memory as {stored}")
            else:
                incident.log("remember", "Not stored - no real fault was found")
        else:
            incident.status = "awaiting_approval"
        await self._emit(incident, "incident.resolved" if resolved else "incident.blocked")

        # Capstone: document the incident. Done after the resolved emit so the
        # card flips to "resolved" instantly and the postmortem streams in a
        # moment later, rather than blocking on a model call.
        if resolved and genuine_fix:
            await self._write_postmortem(incident)
        return incident

    async def reject(self, incident_id: str, reason: str = "") -> Incident:
        incident = self.incidents[incident_id]
        incident.status = "open"
        incident.log("propose", f"Fix rejected by on-call{': ' + reason if reason else ''}")
        await self._emit(incident, "incident.rejected")
        return incident

    async def escalate(self, incident_id: str) -> Incident:
        """File a ticket for a finding Sentinel diagnosed but won't act on.

        This is the hand-off for read-only sources: Sentinel can't (and must not)
        touch an account it only observes, so the next step is a well-formed
        ticket carrying the diagnosis and a suggested manual runbook. Idempotent -
        one ticket per incident, so a double-click never files a duplicate.
        """
        incident = self.incidents[incident_id]
        if incident.ticket:
            return incident
        ticket = ticket_desk.create(incident)
        incident.ticket = ticket
        incident.log(
            "escalate",
            f"Ticket {ticket.id} filed ({ticket.priority}) -> {ticket.assignee}",
            ticket=ticket.model_dump(mode="json"),
        )
        await self._emit(incident, "incident.escalated")
        return incident

    # ----------------------------------------------------------------- chat

    async def ask(self, question: str, incident_id: str | None = None) -> str:
        incident = self.incidents.get(incident_id) if incident_id else None
        health = await self._service_health()
        state: dict[str, Any] = {
            "now": time.strftime("%H:%M:%S"),
            "watching": settings.sample_service_url,
            "service_health": health,
            "open_incidents": [
                {
                    "id": i.id,
                    "title": i.title,
                    "status": i.status,
                    "severity": i.severity.value,
                    "opened": time.strftime("%H:%M:%S", time.localtime(i.opened_at)),
                }
                for i in self.recent(8)
            ],
            "focused_incident": incident.model_dump(mode="json") if incident else None,
            "live_metrics": self.metric_snapshot(),
        }
        hits = await memory.search(question, k=3)
        return await answer_question(question, state, hits)

    # ------------------------------------------------------------- read-side

    def recent(self, limit: int = 20) -> list[Incident]:
        return [self.incidents[i] for i in self.order[:limit]]

    def _open_incident_for(self, anomaly: Anomaly) -> Incident | None:
        # An open incident for the same service that shares at least one signal
        # is the *same ongoing situation*, not a new page: a latency spike that a
        # tick later also trips 5xx is one incident evolving, so refresh it in
        # place. Only a genuinely independent signal set (e.g. a separate memory
        # leak) opens a second card.
        names = {s.name for s in anomaly.signals}
        for incident_id in self.order:
            incident = self.incidents[incident_id]
            if incident.status not in self.OPEN_STATUSES:
                continue
            if not incident.anomaly or incident.service != anomaly.service:
                continue
            if names & {s.name for s in incident.anomaly.signals}:
                return incident
        return None

    # Gauges that represent a current state, not a rate. Averaging these over
    # the window makes them lag reality - active_faults would read ~0 for the
    # first 30s after a fault is injected - so we report their latest sample.
    LATEST_VALUE_METRICS = {"active_faults"}

    def metric_snapshot(self) -> dict[str, Any]:
        now = time.time()
        snapshot: dict[str, Any] = {}
        for (service, name), window in self.detector.metrics.items():
            recent = window.values(since=now - 30)
            if not recent:
                continue
            # Windows are keyed by (service, metric); flatten to a string key the
            # dashboard and JSON can use. The local sample service keeps its bare
            # names (latency_ms, active_faults, ...); other sources are suffixed
            # so a real AWS account's error_rate never overwrites the sample's.
            key = name if service == "sample-service" else f"{name}@{service}"
            if name in self.LATEST_VALUE_METRICS:
                snapshot[key] = round(recent[-1], 3)
            else:
                snapshot[key] = round(sum(recent) / len(recent), 3)
        return snapshot

    def series(self, name: str, points: int = 120) -> list[dict[str, float]]:
        # Prefer the sample service's copy of the metric; fall back to whichever
        # source owns it. Keys are (service, metric) tuples now.
        window = self.detector.metrics.get(("sample-service", name))
        if window is None:
            window = next(
                (w for (_svc, n), w in self.detector.metrics.items() if n == name),
                None,
            )
        if not window:
            return []
        return [{"ts": ts, "value": v} for ts, v in list(window.samples)[-points:]]

    def status(self) -> dict[str, Any]:
        # "Active faults" as Sentinel sees them: incidents that passed triage as
        # a real problem and are still open (any source, AWS included). Noise is
        # dismissed and never counted; resolved/dismissed incidents drop off. The
        # sample service's injected-flag gauge lives separately in `metrics`.
        active_incidents = sum(
            1 for i in self.incidents.values() if i.status in self.OPEN_STATUSES
        )
        return {
            "online": settings.online,
            "uptime_s": round(time.time() - self.started_at, 1),
            "watching": settings.sample_service_url,
            "sandbox": settings.sandbox_mode,
            "auto_approve": settings.auto_approve,
            "memory_size": memory.count(),
            "incidents": len(self.incidents),
            "active_incidents": active_incidents,
            "metrics": self.metric_snapshot(),
            "inference": ledger.summary(),
        }

    # ------------------------------------------------------------- internals

    async def _write_postmortem(self, incident: Incident) -> None:
        """Generate a Markdown postmortem, save it to incidents/, and surface it."""
        try:
            markdown = await write_postmortem(incident)
        except Exception as exc:  # noqa: BLE001 - documentation must never break resolution
            log.warning("Postmortem generation failed for %s: %s", incident.id, exc)
            return
        incident.postmortem = markdown
        try:
            directory = settings.incidents_dir
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(incident.opened_at))
            path = directory / f"postmortem-{stamp}-{incident.id}.md"
            path.write_text(markdown, encoding="utf-8")
            incident.postmortem_path = str(path.relative_to(directory.parent))
            incident.log("postmortem", f"Postmortem written to {incident.postmortem_path}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not write postmortem file for %s: %s", incident.id, exc)
            incident.log("postmortem", "Postmortem generated (file write failed)")
        await self._emit(incident, "incident.documented")

    async def _emit(self, incident: Incident, event_type: str) -> None:
        await bus.publish(event_type, incident.model_dump(mode="json"))

    async def _service_health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                response = await http.get(f"{settings.sample_service_url}/health")
                return response.json()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"health endpoint unreachable: {exc}"}


sentinel = Sentinel()
