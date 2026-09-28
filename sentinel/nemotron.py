"""Nemotron access through Nebius Token Factory.

Three things here are not obvious and are worth reading before you change them:

1. The Nemotron models on Token Factory are *reasoning* models. They stream
   their chain of thought into `message.reasoning_content` and frequently leave
   `message.content` empty or truncated if `max_tokens` is small. Code that
   reads only `.content` silently gets blank answers. `_extract` handles both.
2. Because of the same behaviour, JSON has to be recovered from a response that
   may be wrapped in prose or fences. `complete_json` brace-matches rather than
   trusting the model to emit clean JSON, and retries once with a repair prompt.
3. Every call is accounted for - model, latency, tokens - so the dashboard can
   show *why* the Nano/Super/Ultra split matters instead of just claiming it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger("sentinel.nemotron")

# Rough public list prices, $ per 1M tokens. Only used for the cost panel in the
# dashboard - update from the Token Factory pricing page if they move.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "nano": (0.10, 0.40),
    "super": (0.35, 1.40),
    "ultra": (0.90, 2.60),
}

TIER_TO_MODEL = {
    "nano": settings.model_nano,
    "super": settings.model_super,
    "ultra": settings.model_ultra,
}


@dataclass
class CallRecord:
    tier: str
    model: str
    purpose: str
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    offline: bool = False

    @property
    def cost_usd(self) -> float:
        inp, out = PRICE_PER_MTOK.get(self.tier, (0.0, 0.0))
        return (self.prompt_tokens * inp + self.completion_tokens * out) / 1_000_000

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "model": self.model,
            "purpose": self.purpose,
            "latency_ms": round(self.latency_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "offline": self.offline,
        }


@dataclass
class Ledger:
    """Running tally of everything Sentinel has spent on inference."""

    calls: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.calls.append(record)

    def summary(self) -> dict[str, Any]:
        by_tier: dict[str, dict[str, float]] = {}
        for call in self.calls:
            row = by_tier.setdefault(
                call.tier, {"calls": 0, "tokens": 0, "cost_usd": 0.0, "latency_ms": 0.0}
            )
            row["calls"] += 1
            row["tokens"] += call.prompt_tokens + call.completion_tokens
            row["cost_usd"] += call.cost_usd
            row["latency_ms"] += call.latency_ms
        for row in by_tier.values():
            row["avg_latency_ms"] = round(row["latency_ms"] / max(row["calls"], 1), 1)
            row["cost_usd"] = round(row["cost_usd"], 6)
            row.pop("latency_ms")
        return {
            "by_tier": by_tier,
            "total_cost_usd": round(sum(c.cost_usd for c in self.calls), 6),
            "total_calls": len(self.calls),
        }


ledger = Ledger()


class NemotronClient:
    """Thin, retrying, tier-aware wrapper over the OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        if settings.online:
            self._client = AsyncOpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=120.0,
                max_retries=0,  # we retry ourselves so we can log each attempt
            )
        elif not settings.allow_offline_fallback:
            raise RuntimeError(
                "NEBIUS_API_KEY is not set and ALLOW_OFFLINE_FALLBACK=false"
            )
        else:
            log.warning("No NEBIUS_API_KEY - running on the offline stub model.")

    @property
    def online(self) -> bool:
        return self._client is not None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _extract(message: Any) -> str:
        """Reasoning models may answer in `content`, `reasoning_content`, or both."""
        content = (getattr(message, "content", None) or "").strip()
        if content:
            return content
        reasoning = (getattr(message, "reasoning_content", None) or "").strip()
        if reasoning:
            log.debug("Falling back to reasoning_content (content was empty)")
        return reasoning

    @staticmethod
    def _first_json_object(text: str) -> dict[str, Any] | None:
        """Pull the first balanced {...} out of arbitrary model prose."""
        start = text.find("{")
        while start != -1:
            depth, in_string, escaped = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find("{", start + 1)
        return None

    # ------------------------------------------------------------------- api

    async def complete(
        self,
        tier: str,
        messages: list[dict[str, str]],
        *,
        purpose: str = "",
        max_tokens: int = 1600,
        temperature: float = 0.2,
        attempts: int = 3,
    ) -> tuple[str, CallRecord]:
        model = TIER_TO_MODEL.get(tier, settings.model_super)
        started = time.perf_counter()

        if not self.online:
            from .offline import offline_completion

            text = offline_completion(purpose, messages)
            record = CallRecord(
                tier=tier,
                model="offline-stub",
                purpose=purpose,
                latency_ms=(time.perf_counter() - started) * 1000,
                offline=True,
            )
            ledger.add(record)
            return text, record

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.chat.completions.create(  # type: ignore[union-attr]
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                text = self._extract(response.choices[0].message)
                usage = getattr(response, "usage", None)
                record = CallRecord(
                    tier=tier,
                    model=model,
                    purpose=purpose,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
                ledger.add(record)
                if not text:
                    raise ValueError("empty completion")
                return text, record
            except Exception as exc:  # noqa: BLE001 - we want to retry anything transient
                last_error = exc
                log.warning("Nemotron %s attempt %d failed: %s", tier, attempt, exc)
                if attempt < attempts:
                    await asyncio.sleep(1.5 * attempt)

        if settings.allow_offline_fallback:
            log.error("Nemotron unreachable, degrading to offline stub: %s", last_error)
            from .offline import offline_completion

            text = offline_completion(purpose, messages)
            record = CallRecord(
                tier=tier,
                model="offline-stub",
                purpose=purpose,
                latency_ms=(time.perf_counter() - started) * 1000,
                offline=True,
            )
            ledger.add(record)
            return text, record
        raise RuntimeError(f"Nemotron call failed after {attempts} attempts") from last_error

    async def complete_json(
        self,
        tier: str,
        messages: list[dict[str, str]],
        *,
        purpose: str = "",
        max_tokens: int = 1600,
        temperature: float = 0.1,
    ) -> tuple[dict[str, Any], CallRecord]:
        text, record = await self.complete(
            tier, messages, purpose=purpose, max_tokens=max_tokens, temperature=temperature
        )
        parsed = self._first_json_object(text)
        if parsed is not None:
            return parsed, record

        repair = messages + [
            {"role": "assistant", "content": text[-2000:]},
            {
                "role": "user",
                "content": "That was not valid JSON. Reply with the JSON object only - no prose, no code fences.",
            },
        ]
        text2, record2 = await self.complete(
            tier, repair, purpose=f"{purpose}:repair", max_tokens=max_tokens, temperature=0.0
        )
        parsed = self._first_json_object(text2)
        if parsed is None:
            raise ValueError(f"Could not parse JSON from {tier} response")
        return parsed, record2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.online:
            from .offline import offline_embedding

            return [offline_embedding(t) for t in texts]
        try:
            response = await self._client.embeddings.create(  # type: ignore[union-attr]
                model=settings.embed_model, input=texts
            )
            return [item.embedding for item in response.data]
        except Exception as exc:  # noqa: BLE001
            log.warning("Embedding call failed (%s) - using local hashing fallback", exc)
            from .offline import offline_embedding

            return [offline_embedding(t) for t in texts]


def load_prompt(name: str) -> str:
    return (settings.prompts_dir / name).read_text(encoding="utf-8")


client = NemotronClient()
