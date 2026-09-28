"""Deliberate fault modes for the demo.

Each fault is a realistic failure shape, not a random error: a leak that only
grows, a dependency that gets slow before it fails, and a query pattern that
multiplies with result-set size. That matters because the detector is supposed
to catch them for different reasons - spike, drift and ratio - and the demo is
only honest if it does.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

FAULTS = ("memory_leak", "dependency_timeout", "n_plus_one")


@dataclass
class FaultState:
    active: set[str] = field(default_factory=set)
    leaked: list[bytes] = field(default_factory=list)
    started_at: dict[str, float] = field(default_factory=dict)

    # runtime config the remediation actions can change
    upstream_timeout_s: float = 8.0
    cache_ttl_s: int = 0
    workers: int = 2

    def enable(self, fault: str) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault: {fault}")
        self.active.add(fault)
        self.started_at[fault] = time.time()

    def clear(self, fault: str = "all") -> None:
        if fault == "all":
            self.active.clear()
            self.started_at.clear()
        else:
            self.active.discard(fault)
            self.started_at.pop(fault, None)
        self.leaked.clear()

    def restart(self) -> None:
        """What a real restart buys you: memory back, root cause untouched."""
        self.leaked.clear()
        self.active.discard("memory_leak")
        self.started_at.pop("memory_leak", None)

    @property
    def healthy(self) -> bool:
        return not self.active


state = FaultState()


def apply_memory_leak() -> None:
    """Retain ~2 MB per request and never release it."""
    if "memory_leak" in state.active:
        state.leaked.append(bytes(20 * 1024 * 1024))


async def apply_dependency_timeout() -> float:
    """Simulate a degrading upstream: slow first, then timing out."""
    if "dependency_timeout" not in state.active:
        await asyncio.sleep(random.uniform(0.01, 0.03))
        return 0.0
    age = time.time() - state.started_at.get("dependency_timeout", time.time())
    # Latency ramps up over the first 30s, then saturates at the configured timeout.
    target = min(0.4 + age * 0.12, state.upstream_timeout_s)
    await asyncio.sleep(min(target, state.upstream_timeout_s))
    return target


def apply_n_plus_one(rows: int = 40) -> int:
    """Return how many DB round-trips this request made."""
    if "n_plus_one" not in state.active:
        return 1
    if state.cache_ttl_s > 0:
        return 2  # cache collapses the fan-out
    return 1 + rows


def memory_rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        # psutil-free fallback: count what we know we leaked.
        return 45.0 + len(state.leaked) * 2.0
