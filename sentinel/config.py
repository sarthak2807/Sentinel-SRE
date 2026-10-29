"""Central configuration. Everything is env-driven so the demo box and CI agree."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Token Factory
    api_key: str = os.getenv("NEBIUS_API_KEY", "").strip()
    base_url: str = os.getenv(
        "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"
    )

    # Nemotron tiers
    model_nano: str = os.getenv(
        "NEMOTRON_NANO", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    )
    model_super: str = os.getenv("NEMOTRON_SUPER", "nvidia/nemotron-3-super-120b-a12b")
    model_ultra: str = os.getenv(
        "NEMOTRON_ULTRA", "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1"
    )
    embed_model: str = os.getenv("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")

    # Runtime
    port: int = int(os.getenv("SENTINEL_PORT", "8080"))
    sample_service_url: str = os.getenv(
        "SAMPLE_SERVICE_URL", "http://127.0.0.1:8000"
    ).rstrip("/")
    db_path: Path = field(default_factory=lambda: ROOT / os.getenv("SENTINEL_DB", "sentinel.db"))

    # Safety
    auto_approve: bool = _bool("AUTO_APPROVE", False)
    allow_offline_fallback: bool = _bool("ALLOW_OFFLINE_FALLBACK", True)
    sandbox_mode: str = os.getenv("SANDBOX_MODE", "local").strip().lower()

    # Detector tuning
    window_seconds: int = int(os.getenv("DETECT_WINDOW_SECONDS", "30"))
    baseline_seconds: int = int(os.getenv("DETECT_BASELINE_SECONDS", "300"))
    min_samples: int = int(os.getenv("DETECT_MIN_SAMPLES", "12"))
    cooldown_seconds: int = int(os.getenv("DETECT_COOLDOWN_SECONDS", "45"))
    # A burst of this many error/warn log lines for one service (by arrival time)
    # is an incident on its own - the path that catches read-only sources like
    # CloudWatch, whose coarse metrics and baseline-less error logs defeat the
    # statistical detector.
    error_log_burst: int = int(os.getenv("DETECT_ERROR_LOG_BURST", "4"))

    # External sources (read-only). AWS CloudWatch is the first adapter; more
    # (Jenkins, GitHub Actions, ...) plug into the same Source protocol.
    aws_enabled: bool = _bool("SENTINEL_AWS_ENABLED", False)
    aws_mode: str = os.getenv("SENTINEL_AWS_MODE", "mock").strip().lower()  # mock | live
    aws_region: str = os.getenv("AWS_REGION", "us-east-1").strip()
    aws_poll_seconds: int = int(os.getenv("SENTINEL_AWS_POLL_SECONDS", "20"))
    aws_log_groups: str = os.getenv("SENTINEL_AWS_LOG_GROUPS", "").strip()
    aws_metrics: str = os.getenv("SENTINEL_AWS_METRICS", "").strip()

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).resolve().parent / "prompts"

    @property
    def incidents_dir(self) -> Path:
        return ROOT / "incidents"

    @property
    def tickets_dir(self) -> Path:
        return ROOT / "tickets"


settings = Settings()
