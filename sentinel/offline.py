"""Offline fallbacks.

A live demo that depends on a network call is a demo that fails on stage. When
no API key is present - or Token Factory is unreachable mid-incident - Sentinel
keeps running on deterministic local stand-ins. Every response produced this way
is tagged `offline: true` in the ledger and in the UI, so nothing pretends to be
model output when it is not.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

EMBED_DIM = 256


def offline_embedding(text: str) -> list[float]:
    """Hashed bag-of-words embedding. Crude, deterministic, dependency-free."""
    vector = [0.0] * EMBED_DIM
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBED_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _last_user(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def offline_completion(purpose: str, messages: list[dict[str, str]]) -> str:
    text = _last_user(messages).lower()

    if purpose.startswith("triage"):
        category = _guess_category(text)
        return json.dumps(
            {
                "is_real": True,
                "severity": "critical" if "error_rate" in text or "memory" in text else "warning",
                "category": category,
                "one_liner": f"Offline triage: signals look like {category.replace('_', ' ')}.",
                "needs_deep_analysis": True,
                "confidence": 0.45,
            }
        )

    if purpose.startswith("diagnose"):
        category = _guess_category(text)
        fix = {
            "memory_leak": ("restart_service", {"reason": "reclaim leaked heap"}),
            "dependency_timeout": ("set_timeout", {"seconds": 2}),
            "n_plus_one_query": ("enable_cache", {"ttl_seconds": 60}),
        }.get(category, ("collect_diagnostics", {}))
        return json.dumps(
            {
                "summary": f"Offline diagnosis: {category.replace('_', ' ')} on sample-service.",
                "root_cause": "Generated without model access - treat as a placeholder.",
                "reasoning": "Pattern-matched from anomaly signal names, not reasoned.",
                "confidence": 0.3,
                "blast_radius": "Unknown without model access.",
                "similar_incidents": [],
                "proposed_fix": {
                    "action": fix[0],
                    "params": fix[1],
                    "rationale": "Default remediation for this signal shape.",
                    "expected_effect": "Signals should return to baseline within a minute.",
                    "rollback": "Re-run inject_fault.py to restore the broken state.",
                    "risk": "low",
                },
            }
        )

    return (
        "Sentinel is running without a Nebius API key, so this answer is a local "
        "placeholder. Set NEBIUS_API_KEY in .env to get real Nemotron reasoning."
    )


def _guess_category(text: str) -> str:
    if "memory" in text or "rss" in text or "heap" in text:
        return "memory_leak"
    if "timeout" in text or "latency" in text or "p95" in text:
        return "dependency_timeout"
    if "query" in text or "n+1" in text or "db_calls" in text:
        return "n_plus_one_query"
    if "error" in text or "5xx" in text:
        return "error_spike"
    return "unknown"


def stub_summary(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2)
