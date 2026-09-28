"""Where a remediation actually runs.

Two backends behind one interface:

* `contree`  - Nebius Token Factory Sandboxes. VM-isolated, branchable, so a fix
               can be trialled on a fork of the environment and rolled back if
               the verification step fails.
* `local`    - a hard-capped subprocess for offline demos. No shell, explicit
               argv only, 20-second timeout, output truncated.

Both return the same `ExecutionResult`, so the pipeline does not care which one
is live and the UI can just say where it ran.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .config import settings
from .models import ExecutionResult

log = logging.getLogger("sentinel.sandbox")

MAX_OUTPUT = 4000
TIMEOUT_S = 20


class Sandbox:
    async def run(self, action_name: str, argv: list[str]) -> ExecutionResult:  # pragma: no cover
        raise NotImplementedError


class LocalSandbox(Sandbox):
    """Restricted subprocess. Never uses shell=True; argv comes from the allowlist."""

    where = "local-subprocess"

    async def run(self, action_name: str, argv: list[str]) -> ExecutionResult:
        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_S)
            return ExecutionResult(
                ok=process.returncode == 0,
                action=action_name,
                where=self.where,
                stdout=stdout.decode(errors="replace")[:MAX_OUTPUT],
                stderr=stderr.decode(errors="replace")[:MAX_OUTPUT],
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                ok=False,
                action=action_name,
                where=self.where,
                stderr=f"timed out after {TIMEOUT_S}s",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except FileNotFoundError as exc:
            return ExecutionResult(
                ok=False, action=action_name, where=self.where, stderr=str(exc)
            )


class ContreeSandbox(Sandbox):
    """Nebius Sandboxes backend.

    Uses the Contree SDK when it is installed and a project is configured.
    Falls back to the local sandbox rather than failing the incident, because a
    missing sandbox should not stop a human from getting their diagnosis.
    """

    where = "nebius-sandbox"

    def __init__(self) -> None:
        self._session = None
        self._fallback = LocalSandbox()

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        try:
            import contree  # type: ignore
        except ImportError:
            log.warning("contree SDK not installed - `pip install contree`. Using local sandbox.")
            return None
        try:
            self._session = await asyncio.to_thread(
                contree.Session.create,
                image="ubuntu:latest",
                project=os.getenv("NEBIUS_AI_PROJECT") or None,
            )
            return self._session
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open a Nebius sandbox (%s) - using local sandbox.", exc)
            return None

    async def run(self, action_name: str, argv: list[str]) -> ExecutionResult:
        session = await self._ensure_session()
        if session is None:
            return await self._fallback.run(action_name, argv)
        started = time.perf_counter()
        try:
            # Fork first: if verification fails we discard the branch instead of
            # leaving a half-applied change behind.
            branch = await asyncio.to_thread(session.fork)
            completed = await asyncio.to_thread(branch.run, argv)
            return ExecutionResult(
                ok=getattr(completed, "exit_code", 1) == 0,
                action=action_name,
                where=self.where,
                stdout=str(getattr(completed, "stdout", ""))[:MAX_OUTPUT],
                stderr=str(getattr(completed, "stderr", ""))[:MAX_OUTPUT],
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Sandbox execution failed (%s) - falling back to local.", exc)
            return await self._fallback.run(action_name, argv)


def get_sandbox() -> Sandbox:
    return ContreeSandbox() if settings.sandbox_mode == "contree" else LocalSandbox()
