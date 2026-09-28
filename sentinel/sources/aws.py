"""AWS CloudWatch source: pull metrics and error logs, normalise to telemetry.

Read-only by design. This module never calls a mutating AWS API - it only reads
CloudWatch metrics (`get_metric_data`) and log events (`filter_log_events`) and
turns them into `TelemetryEvent`s. Remediation against a real account is
deliberately out of scope; incidents from here are diagnosed, not acted on.

Two modes:
  * ``live`` - real boto3 against the configured account/region.
  * ``mock`` - replays a realistic latency/5xx spike so the demo works with no
    AWS credentials at all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..config import settings
from ..models import TelemetryEvent

log = logging.getLogger("sentinel.sources.aws")

# Base metric names the detector already understands (units/thresholds keyed off
# these); AWS metrics are published as "<base>:aws/<label>" so they never collide
# with the sample service's own metrics while staying detector-friendly.
_KNOWN_BASES = {"latency_ms", "http_5xx", "error_rate", "errors", "memory_rss_mb"}


@dataclass
class MetricSpec:
    namespace: str        # e.g. AWS/ApplicationELB
    metric: str           # e.g. TargetResponseTime
    stat: str             # e.g. Average / Sum
    label: str            # human label, e.g. prod-alb
    as_name: str          # detector base name, e.g. latency_ms
    dimensions: dict[str, str]

    @property
    def event_name(self) -> str:
        base = self.as_name if self.as_name in _KNOWN_BASES else self.as_name
        return f"{base}:aws/{self.label}"


def parse_metric_specs(raw: str) -> list[MetricSpec]:
    """Parse SENTINEL_AWS_METRICS.

    Format (comma-separated entries):
        Namespace:Metric:Stat:Label:AsName[:Dim=Val;Dim=Val]
    """
    specs: list[MetricSpec] = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        parts = entry.split(":")
        if len(parts) < 5:
            log.warning("Ignoring malformed metric spec: %s", entry)
            continue
        namespace, metric, stat, label, as_name = parts[:5]
        dims: dict[str, str] = {}
        if len(parts) >= 6 and parts[5]:
            for pair in parts[5].split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    dims[k.strip()] = v.strip()
        specs.append(
            MetricSpec(namespace, metric, stat, label, as_name, dims)
        )
    return specs


class CloudWatchSource:
    name = "aws-cloudwatch"

    def __init__(self) -> None:
        self.mode = settings.aws_mode
        self.region = settings.aws_region
        self.poll_seconds = max(5, settings.aws_poll_seconds)
        self.specs = parse_metric_specs(settings.aws_metrics)
        self.log_groups = [g.strip() for g in settings.aws_log_groups.split(",") if g.strip()]
        self._last_log_ts: dict[str, int] = {}
        self.service = f"aws:{self.region}"

    async def run(self, ingest) -> None:
        if self.mode == "mock":
            await self._run_mock(ingest)
        else:
            await self._run_live(ingest)

    # ------------------------------------------------------------------ live

    async def _run_live(self, ingest) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError:
            log.error("boto3 not installed; run `pip install boto3` or use SENTINEL_AWS_MODE=mock")
            return
        import boto3

        cw = boto3.client("cloudwatch", region_name=self.region)
        logs = boto3.client("logs", region_name=self.region)
        log.info("AWS CloudWatch source live in %s (%d metrics, %d log groups)",
                 self.region, len(self.specs), len(self.log_groups))
        while True:
            try:
                await self._poll_metrics(cw, ingest)
                await self._poll_logs(logs, ingest)
            except Exception:  # noqa: BLE001
                log.exception("CloudWatch poll failed")
            await asyncio.sleep(self.poll_seconds)

    async def _poll_metrics(self, cw, ingest) -> None:
        if not self.specs:
            return
        now = time.time()
        queries = [
            {
                "Id": f"m{i}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": s.namespace,
                        "MetricName": s.metric,
                        "Dimensions": [{"Name": k, "Value": v} for k, v in s.dimensions.items()],
                    },
                    "Period": 60,
                    "Stat": s.stat,
                },
                "ReturnData": True,
            }
            for i, s in enumerate(self.specs)
        ]
        resp = await asyncio.to_thread(
            cw.get_metric_data,
            MetricDataQueries=queries,
            StartTime=_dt(now - 600),
            EndTime=_dt(now),
        )
        by_id = {r["Id"]: r for r in resp.get("MetricDataResults", [])}
        for i, spec in enumerate(self.specs):
            result = by_id.get(f"m{i}")
            if not result:
                continue
            for ts, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                await ingest(TelemetryEvent(
                    ts=ts.timestamp() if hasattr(ts, "timestamp") else float(ts),
                    service=self.service,
                    kind="metric",
                    name=spec.event_name,
                    value=float(value),
                    attrs={"source": "cloudwatch", "aws_metric": spec.metric, "label": spec.label},
                ))

    async def _poll_logs(self, logs, ingest) -> None:
        now_ms = int(time.time() * 1000)
        for group in self.log_groups:
            start = self._last_log_ts.get(group, now_ms - 5 * 60 * 1000)
            try:
                resp = await asyncio.to_thread(
                    logs.filter_log_events,
                    logGroupName=group,
                    startTime=start + 1,
                    filterPattern="?ERROR ?Error ?error ?WARN ?Exception ?Traceback",
                    limit=100,
                )
            except Exception:  # noqa: BLE001
                log.exception("filter_log_events failed for %s", group)
                continue
            for ev in resp.get("events", []):
                msg = (ev.get("message") or "").strip()
                ts_ms = int(ev.get("timestamp", now_ms))
                self._last_log_ts[group] = max(self._last_log_ts.get(group, 0), ts_ms)
                await ingest(TelemetryEvent(
                    ts=ts_ms / 1000.0,
                    service=self.service,
                    kind="log",
                    name="cloudwatch_logs",
                    level="warn" if ("WARN" in msg.upper()) else "error",
                    message=msg[:500],
                    attrs={"source": "cloudwatch", "log_group": group},
                ))

    # ------------------------------------------------------------------ mock

    async def _run_mock(self, ingest) -> None:
        """Replay a realistic transient: healthy baseline, a latency+5xx spike,
        then recovery - so an AWS-badged incident opens with no credentials."""
        label = self.specs[0].label if self.specs else "prod-alb"
        lat_name = f"latency_ms:aws/{label}"
        err_name = f"http_5xx:aws/{label}"
        now = time.time()
        # Seed a baseline older than the detection window so a spike stands out.
        for age in range(int(settings.baseline_seconds), int(settings.window_seconds), -5):
            await ingest(TelemetryEvent(
                ts=now - age, service=self.service, kind="metric",
                name=lat_name, value=30.0 + (age % 7),
                attrs={"source": "cloudwatch-mock", "label": label},
            ))
        log.info("AWS CloudWatch source in MOCK mode (label=%s) - baseline seeded", label)
        start = time.time()
        while True:
            elapsed = time.time() - start
            spiking = 15 <= elapsed <= 75  # a ~1 minute incident window
            latency = 950.0 + (elapsed % 5) * 20 if spiking else 30.0 + (elapsed % 7)
            await ingest(TelemetryEvent(
                service=self.service, kind="metric", name=lat_name,
                value=latency, attrs={"source": "cloudwatch-mock", "label": label},
            ))
            await ingest(TelemetryEvent(
                service=self.service, kind="metric", name=err_name,
                value=12.0 if spiking else 0.0,
                attrs={"source": "cloudwatch-mock", "label": label},
            ))
            if spiking:
                await ingest(TelemetryEvent(
                    service=self.service, kind="log", level="error",
                    name="cloudwatch_logs",
                    message=f"upstream 5xx on {label}: target group returned 502 (mock)",
                    attrs={"source": "cloudwatch-mock", "log_group": f"/aws/{label}"},
                ))
            await asyncio.sleep(2)


def _dt(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)
