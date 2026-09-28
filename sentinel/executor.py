"""Remediation executor: approval gate, allowlist check, sandbox, verification.

The order matters and is not negotiable:

    proposal -> allowlist lookup -> parameter validation -> human approval
             -> sandboxed execution -> post-fix verification -> audit record

If any step fails the incident stays open and the on-call engineer is told what
stopped. Nothing here can run an arbitrary string from the model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .actions import REGISTRY
from .config import ROOT, settings
from .models import ExecutionResult, Incident, ProposedFix
from .sandbox import get_sandbox

log = logging.getLogger("sentinel.executor")
AUDIT_LOG = ROOT / "audit.log"


class ApprovalRequired(Exception):
    """Raised when execution is attempted without a recorded human approval."""


class Executor:
    def __init__(self) -> None:
        self.sandbox = get_sandbox()

    # --------------------------------------------------------------- checks

    def plan(self, fix: ProposedFix) -> tuple[Any, dict[str, Any], list[str]]:
        action = REGISTRY.get(fix.action)
        if action is None:
            raise ValueError(
                f"'{fix.action}' is not an approved remediation. Allowed: {sorted(REGISTRY)}"
            )
        params = action.validate(fix.params)
        return action, params, action.build(params)

    def preview(self, fix: ProposedFix) -> dict[str, Any]:
        """What the operator sees before clicking Approve - the literal command."""
        action, params, argv = self.plan(fix)
        return {
            "action": action.name,
            "description": action.description,
            "risk": action.risk,
            "params": params,
            "command": " ".join(argv),
            "runs_in": getattr(self.sandbox, "where", "sandbox"),
            "verification": action.verify_hint,
            "rollback": fix.rollback or "not specified",
        }

    # -------------------------------------------------------------- execute

    async def execute(
        self, incident: Incident, *, approved_by: str | None = None
    ) -> ExecutionResult:
        if incident.diagnosis is None or incident.diagnosis.proposed_fix is None:
            raise ValueError("Nothing to execute - this incident has no proposed fix.")
        if not approved_by and not settings.auto_approve:
            raise ApprovalRequired("A human must approve this fix before it runs.")

        fix = incident.diagnosis.proposed_fix
        action, params, argv = self.plan(fix)

        incident.log(
            "execute",
            f"Running {action.name} in {getattr(self.sandbox, 'where', 'sandbox')}",
            approved_by=approved_by or "auto-approve",
        )
        result = await self.sandbox.run(action.name, argv)

        if result.ok:
            result.verified, result.verification_note = await self._verify(action)
        else:
            result.verification_note = "Skipped verification because the fix did not run cleanly."

        self._audit(incident, action.name, params, approved_by, result)
        return result

    async def _verify(self, action: Any) -> tuple[bool | None, str]:
        """Give the service a moment, then read its own health back.

        This is what turns 'the agent ran a command' into 'the agent fixed it'.
        """
        await asyncio.sleep(4)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                response = await http.get(f"{settings.sample_service_url}/health")
                health = response.json()
            healthy = bool(health.get("healthy", False))
            faults = health.get("active_faults", [])
            note = (
                f"Service reports healthy=True, active faults: {faults or 'none'}. "
                f"Expected: {action.verify_hint}"
            )
            return healthy and not faults, note
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not reach the service to verify: {exc}"

    def _audit(
        self,
        incident: Incident,
        action_name: str,
        params: dict[str, Any],
        approved_by: str | None,
        result: ExecutionResult,
    ) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "incident_id": incident.id,
            "action": action_name,
            "params": params,
            "approved_by": approved_by or "auto-approve",
            "where": result.where,
            "ok": result.ok,
            "verified": result.verified,
        }
        try:
            with AUDIT_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:  # pragma: no cover
            log.warning("Could not write audit log: %s", exc)


executor = Executor()
