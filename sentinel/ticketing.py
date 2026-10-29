"""Escalation desk: turn a diagnosed incident Sentinel won't act on into a ticket.

Read-only sources (a real AWS account) are observed, not remediated - so the
agent's job ends at a clean hand-off: a ticket carrying the diagnosis, blast
radius and a *suggested* runbook a human can act on. This desk files a local
ticket (an ID plus a markdown artifact on disk). A real Jira / ServiceNow /
GitHub backend drops in behind the same `create()` call later.
"""
from __future__ import annotations

import logging
import time
import uuid

from .config import settings
from .models import Incident, Severity, Ticket

log = logging.getLogger("sentinel.ticketing")

# Map how urgent Sentinel thinks it is to a priority an on-call rotation reads.
_PRIORITY = {Severity.critical: "P1", Severity.warning: "P3", Severity.info: "P4"}


class TicketDesk:
    backend = "local"

    def create(self, incident: Incident) -> Ticket:
        ticket = Ticket(
            id="SENT-" + uuid.uuid4().hex[:6].upper(),
            priority=_PRIORITY.get(incident.severity, "P3"),
            summary=incident.title or "Incident",
            backend=self.backend,
        )
        ticket.body = self._render(incident, ticket)
        self._write(ticket)
        return ticket

    def _render(self, inc: Incident, ticket: Ticket) -> str:
        d = inc.diagnosis
        source = (
            "read-only (observed; Sentinel ran no action against the account)"
            if inc.read_only else "owned by Sentinel"
        )
        lines = [
            f"# [{ticket.id}] {inc.title or 'Incident'}",
            "",
            f"- **Priority:** {ticket.priority}",
            f"- **Service:** {inc.service}",
            f"- **Severity:** {inc.severity.value}",
            f"- **Assignee:** {ticket.assignee}",
            f"- **Opened:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(inc.opened_at))}",
            f"- **Source:** {source}",
            "",
        ]
        if d:
            lines += [
                "## Summary", d.summary, "",
                "## Root cause", d.root_cause, "",
                f"**Blast radius:** {d.blast_radius or 'not established'}",
                f"**Confidence:** {round((d.confidence or 0) * 100)}%", "",
            ]
            if d.reasoning:
                lines += ["## Reasoning", d.reasoning, ""]
            if d.proposed_fix:
                f = d.proposed_fix
                lines += [
                    "## Suggested remediation (manual - Sentinel will not run this)",
                    f"- **Action:** {f.action}",
                    f"- **Rationale:** {f.rationale}",
                    f"- **Expected effect:** {f.expected_effect}",
                    f"- **Rollback:** {f.rollback}",
                    f"- **Risk:** {f.risk}", "",
                ]
        if inc.anomaly and inc.anomaly.evidence_logs:
            lines += ["## Evidence", "```"] + inc.anomaly.evidence_logs[:12] + ["```", ""]
        lines += ["---", f"Filed by Sentinel - AI SRE Copilot. Incident {inc.id}."]
        return "\n".join(lines)

    def _write(self, ticket: Ticket) -> None:
        try:
            directory = settings.tickets_dir
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{ticket.id}.md"
            path.write_text(ticket.body, encoding="utf-8")
            ticket.path = str(path.relative_to(directory.parent))
        except Exception as exc:  # noqa: BLE001 - a ticket in memory still beats none
            log.warning("Could not write ticket file %s: %s", ticket.id, exc)


desk = TicketDesk()
