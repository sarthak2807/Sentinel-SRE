"""The remediation allowlist.

This file is the single most important safety boundary in Sentinel. The model
never gets to emit a shell command; it gets to pick a *name* from this registry
and fill in typed parameters. Anything it invents fails validation before it
reaches the sandbox.

Adding a capability is a deliberate, reviewable act: one entry here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import settings


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    risk: str
    params_schema: dict[str, str]
    # Rendered into a shell command that runs inside the sandbox.
    build: Callable[[dict[str, Any]], list[str]]
    verify_hint: str = ""

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, type_name in self.params_schema.items():
            if key not in params:
                continue
            value = params[key]
            try:
                cleaned[key] = {"int": int, "float": float, "str": str}[type_name](value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{self.name}: parameter '{key}' must be {type_name}") from exc
        unknown = set(params) - set(self.params_schema)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        return cleaned


def _curl(path: str, payload: str = "") -> list[str]:
    url = f"{settings.sample_service_url}{path}"
    cmd = ["curl", "-fsS", "-X", "POST", url]
    if payload:
        cmd += ["-H", "Content-Type: application/json", "-d", payload]
    return cmd


REGISTRY: dict[str, Action] = {}


def register(action: Action) -> Action:
    REGISTRY[action.name] = action
    return action


register(
    Action(
        name="restart_service",
        description="Restart the target service's worker to reclaim leaked memory or wedged state.",
        risk="medium",
        params_schema={"reason": "str"},
        build=lambda p: _curl("/admin/restart"),
        verify_hint="memory_rss_mb should drop back to its starting value within 30s",
    )
)

register(
    Action(
        name="set_timeout",
        description="Lower the outbound call timeout so a slow dependency stops occupying workers.",
        risk="low",
        params_schema={"seconds": "float"},
        build=lambda p: _curl("/admin/config", f'{{"upstream_timeout_s": {p.get("seconds", 2)}}}'),
        verify_hint="p95 latency_ms should fall below the pre-incident baseline",
    )
)

register(
    Action(
        name="enable_cache",
        description="Turn on the per-request query cache to collapse an N+1 query pattern.",
        risk="low",
        params_schema={"ttl_seconds": "int"},
        build=lambda p: _curl("/admin/config", f'{{"cache_ttl_s": {p.get("ttl_seconds", 60)}}}'),
        verify_hint="db_calls_per_request should return to single digits",
    )
)

register(
    Action(
        name="scale_workers",
        description="Add worker capacity to absorb a load spike.",
        risk="medium",
        params_schema={"count": "int"},
        build=lambda p: _curl("/admin/config", f'{{"workers": {p.get("count", 4)}}}'),
        verify_hint="latency_ms should recover while error_rate stays flat",
    )
)

register(
    Action(
        name="clear_fault",
        description="Disable the injected fault flag on the sample service (demo-only reset).",
        risk="low",
        params_schema={"fault": "str"},
        build=lambda p: _curl("/admin/clear_fault", f'{{"fault": "{p.get("fault", "all")}"}}'),
        verify_hint="all signals return to baseline",
    )
)

register(
    Action(
        name="collect_diagnostics",
        description="Read-only: capture the service's health snapshot for a human to review.",
        risk="low",
        params_schema={},
        build=lambda p: ["curl", "-fsS", f"{settings.sample_service_url}/health"],
        verify_hint="no change expected - this is an information-gathering step",
    )
)


def describe_for_prompt() -> str:
    """The menu handed to the model. Keep it terse - it goes in every prompt."""
    lines = []
    for action in REGISTRY.values():
        params = ", ".join(f"{k}: {v}" for k, v in action.params_schema.items()) or "none"
        lines.append(f"- {action.name} (risk: {action.risk}; params: {params}) - {action.description}")
    return "\n".join(lines)
