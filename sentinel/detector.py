"""Anomaly detection over the telemetry stream.

Design notes
------------
The detector is deliberately *not* an LLM. Running a model on every log line is
slow, expensive and non-deterministic; the model's job starts once something is
already known to be odd. So this stage is plain statistics:

* a rolling `baseline` window (default 5 min) and a short `recent` window (30 s)
* robust z-scores using median / MAD, so one 8-second GC pause does not poison
  the baseline the way a mean/stddev would
* a monotonic-growth check for memory, which never spikes - it creeps

It also refuses to fire while it is still learning (`min_samples`) and holds a
per-fingerprint cooldown, because an SRE tool that pages twice a minute is worse
than no tool at all.
"""
from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from typing import Deque, Iterable

from .config import settings
from .models import Anomaly, Signal, TelemetryEvent

Sample = tuple[float, float]  # (timestamp, value)


def _median(values: Iterable[float]) -> float:
    data = list(values)
    return statistics.median(data) if data else 0.0


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation, scaled to be comparable to a stddev."""
    if not values:
        return 0.0
    return 1.4826 * statistics.median([abs(v - centre) for v in values])


class MetricWindow:
    def __init__(self, horizon: int) -> None:
        self.horizon = horizon
        self.samples: Deque[Sample] = deque()

    def add(self, ts: float, value: float) -> None:
        self.samples.append((ts, value))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        while self.samples and now - self.samples[0][0] > self.horizon:
            self.samples.popleft()

    def values(self, since: float | None = None) -> list[float]:
        if since is None:
            return [v for _, v in self.samples]
        return [v for ts, v in self.samples if ts >= since]


class AnomalyDetector:
    """Consumes TelemetryEvents, emits Anomalies."""

    def __init__(self) -> None:
        # Windows are keyed by (service, metric name), not name alone. The
        # aggregate metrics (error_rate, http_5xx, latency_ms) are derived from
        # *every* event, so a shared-by-name window would blend a real AWS
        # account's error logs into the sample service's error_rate - and hand
        # the metric to whichever source wrote last. Keying by owning service
        # keeps each system's baseline and signals cleanly separated.
        self.metrics: dict[tuple[str, str], MetricWindow] = defaultdict(
            lambda: MetricWindow(settings.baseline_seconds)
        )
        self.recent_logs: Deque[TelemetryEvent] = deque(maxlen=400)
        # Wall-clock arrival of each log line: (received_at, service, level). Kept
        # separate from the event's own `ts` so a source that backdates its
        # timestamps - a real CloudWatch account lags one or two minutes - is still
        # judged on when its errors actually reached us.
        self._log_arrivals: Deque[tuple[float, str, str]] = deque(maxlen=400)
        self._last_fired: dict[str, float] = {}
        self.service = "sample-service"

    # ------------------------------------------------------------ ingestion

    def observe(self, event: TelemetryEvent) -> None:
        service = event.service or "sample-service"
        self.service = service
        if event.kind == "log":
            self.recent_logs.append(event)
            self._log_arrivals.append((time.time(), service, event.level))
            # Logs are also a metric: error rate.
            self._track(service, "error_rate", event.ts, 1.0 if event.level == "error" else 0.0)
        if event.duration_ms is not None:
            self._track(service, "latency_ms", event.ts, event.duration_ms)
            if event.route:
                self._track(service, f"latency_ms:{event.route}", event.ts, event.duration_ms)
        if event.status is not None:
            self._track(service, "http_5xx", event.ts, 1.0 if event.status >= 500 else 0.0)
        if event.kind == "metric" and event.value is not None:
            self._track(service, event.name, event.ts, event.value)
        for key, value in event.attrs.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._track(service, key, event.ts, float(value))

    def _track(self, service: str, name: str, ts: float, value: float) -> None:
        self.metrics[(service, name)].add(ts, value)

    # ------------------------------------------------------------ detection

    def evaluate(self, now: float | None = None) -> list[Anomaly]:
        now = now or time.time()
        cutoff = now - settings.window_seconds
        graded: list[tuple[str, Signal]] = []

        # Per service: skip the aggregate latency_ms only when that same service
        # also reports per-route latency to supersede it; otherwise the aggregate
        # is the only latency we have for it.
        services_with_per_route = {
            svc for (svc, n) in self.metrics if n.startswith("latency_ms:")
        }

        for (service, name), window in self.metrics.items():
            if name == "latency_ms" and service in services_with_per_route:
                continue
            window._trim(now)
            recent = window.values(since=cutoff)
            baseline = [v for ts, v in window.samples if ts < cutoff]
            if len(recent) < 3 or len(baseline) < settings.min_samples:
                continue

            if name in RATE_METRICS:
                # A rate is only meaningful as an average: one error in a window
                # of two hundred requests IS the signal we want to measure.
                observed = sum(recent) / len(recent)
                centre = sum(baseline) / len(baseline)
                spread = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
            else:
                # For level metrics the median is what keeps one 8-second GC
                # pause from looking like a sustained latency incident.
                observed = _median(recent)
                centre = _median(baseline)
                spread = _mad(baseline, centre)

            # Guard against a perfectly flat baseline turning every wobble into
            # a 40-sigma event.
            spread = max(spread, abs(centre) * 0.08, _epsilon_for(name))
            z = (observed - centre) / spread

            if z >= _threshold_for(name):
                graded.append((service, Signal(
                    name=name,
                    observed=round(observed, 3),
                    baseline=round(centre, 3),
                    zscore=round(z, 2),
                    unit=_unit_for(name),
                    note=_note_for(name, observed, centre),
                )))
            elif name in MONOTONIC_METRICS:
                trend = self._growth_signal(name, window, now)
                if trend:
                    graded.append((service, trend))

        # Error logs are a signal in their own right. The statistical path above
        # needs a dense, baselined metric stream - which a read-only source like
        # CloudWatch never gives us: its metrics are one-per-minute and its error
        # logs have no healthy baseline to deviate from. So treat a burst of
        # freshly-arrived error/warn lines for a service as its own anomaly,
        # keyed on arrival time so an account that lags minutes still counts.
        log_cutoff = now - settings.window_seconds
        errors_by_service: dict[str, int] = defaultdict(int)
        for arrival, svc, level in self._log_arrivals:
            if arrival >= log_cutoff and level in {"error", "warn"}:
                errors_by_service[svc] += 1
        services_already_firing_errors = {
            svc for svc, s in graded if s.name == "error_rate"
        }
        for svc, count in errors_by_service.items():
            if count < settings.error_log_burst or svc in services_already_firing_errors:
                continue
            graded.append((svc, Signal(
                name="error_logs",
                observed=float(count),
                baseline=0.0,
                zscore=float(count),
                unit="logs",
                note=f"{count} error/warn log lines in the last {settings.window_seconds}s",
            )))

        if not graded:
            return []

        # Group signals by their owning service, so a spike on a real AWS target
        # group is never merged into the same incident as an unrelated
        # sample-service signal - and each source gets a stable fingerprint the
        # per-fingerprint cooldown can actually dedupe.
        groups: dict[str, list[Signal]] = defaultdict(list)
        for service, s in graded:
            groups[service].append(s)

        anomalies: list[Anomaly] = []
        for service, sigs in groups.items():
            anomaly = Anomaly(
                service=service,
                title=_title_for(sigs),
                signals=sorted(sigs, key=lambda s: -s.zscore),
                evidence_logs=self._evidence(cutoff, service),
                window_seconds=settings.window_seconds,
            )
            key = anomaly.fingerprint()
            if now - self._last_fired.get(key, 0.0) < settings.cooldown_seconds:
                continue
            self._last_fired[key] = now
            anomalies.append(anomaly)
        return anomalies

    # ------------------------------------------------------------- internals

    def _growth_signal(self, name: str, window: MetricWindow, now: float) -> Signal | None:
        """Catch slow, monotonic climbs that never look like a spike."""
        samples = [(ts, v) for ts, v in window.samples if now - ts <= settings.baseline_seconds]
        if len(samples) < settings.min_samples:
            return None
        half = len(samples) // 2
        first = [v for _, v in samples[:half]]
        second = [v for _, v in samples[half:]]
        start, end = _median(first), _median(second)
        if start <= 0:
            return None
        growth = (end - start) / start
        rising = sum(1 for a, b in zip(second, second[1:]) if b >= a) / max(len(second) - 1, 1)
        if growth > 0.25 and rising > 0.8:
            return Signal(
                name=name,
                observed=round(end, 2),
                baseline=round(start, 2),
                zscore=round(growth * 10, 2),
                unit=_unit_for(name),
                note=f"rose {growth:.0%} over the last {settings.baseline_seconds}s and never came back down",
            )
        return None

    def _evidence(self, cutoff: float, service: str | None = None) -> list[str]:
        """The log lines a human would actually scroll to first, scoped to the
        service the anomaly belongs to so AWS logs don't leak into a
        sample-service card (or vice versa)."""
        logs = self.recent_logs
        if service is not None:
            scoped = [e for e in logs if (e.service or "sample-service") == service]
            logs = scoped or logs
        errors = [e for e in logs if e.ts >= cutoff and e.level in {"error", "warn"}]
        chosen = (errors or list(logs))[-12:]
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(e.ts))} {e.level.upper():5} {e.route or e.name} {e.message}"
            for e in chosen
        ]


RATE_METRICS = {"error_rate", "http_5xx"}

MONOTONIC_METRICS = {"memory_rss_mb", "open_connections", "goroutines", "heap_mb"}

_THRESHOLDS = {
    "error_rate": 2.5,
    "http_5xx": 2.5,
    "latency_ms": 3.0,
    "db_calls_per_request": 2.5,
}
_UNITS = {
    "latency_ms": "ms",
    "memory_rss_mb": "MB",
    "error_rate": "ratio",
    "http_5xx": "ratio",
    "db_calls_per_request": "queries/req",
}

def _threshold_for(name: str) -> float:
    key = "latency_ms" if name.startswith("latency_ms") else name
    return _THRESHOLDS.get(key, 3.5)


def _epsilon_for(name: str) -> float:
    key = "latency_ms" if name.startswith("latency_ms") else name
    return {"error_rate": 0.02, "http_5xx": 0.02, "latency_ms": 5.0}.get(key, 0.5)


def _unit_for(name: str) -> str:
    key = "latency_ms" if name.startswith("latency_ms") else name
    return _UNITS.get(key, "")


def _note_for(name: str, observed: float, baseline: float) -> str:
    if baseline <= 0:
        return f"{name} appeared where there was previously none"
    return f"{observed:.2f} vs a normal {baseline:.2f} ({observed / baseline:.1f}x)"


def _title_for(signals: list[Signal]) -> str:
    top = max(signals, key=lambda s: s.zscore)
    friendly = {
        "error_rate": "Error rate spike",
        "http_5xx": "5xx responses climbing",
        "latency_ms": "Latency jump",
        "memory_rss_mb": "Memory growing without release",
        "db_calls_per_request": "Query count per request exploded",
        "error_logs": "Errors in the logs",
    }
    return friendly.get(top.name, f"{top.name} out of band")
