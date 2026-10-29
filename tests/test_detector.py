"""The detector is the one stage with no model in it, so it can be tested hard."""
import time

import pytest

from sentinel.config import settings
from sentinel.detector import AnomalyDetector
from sentinel.models import TelemetryEvent


def feed(detector, *, seconds, rate_hz=4, **event_kwargs):
    now = time.time()
    start = now - seconds
    step = 1.0 / rate_hz
    ts = start
    while ts < now:
        detector.observe(TelemetryEvent(ts=ts, **event_kwargs))
        ts += step
    return now


def test_steady_traffic_produces_no_anomaly():
    d = AnomalyDetector()
    feed(d, seconds=settings.baseline_seconds, kind="log", level="info",
         route="/api/orders", status=200, duration_ms=40)
    assert d.evaluate() == []


def test_latency_spike_is_detected():
    d = AnomalyDetector()
    now = time.time()
    # 5 minutes of healthy baseline
    ts = now - settings.baseline_seconds
    while ts < now - settings.window_seconds:
        d.observe(TelemetryEvent(ts=ts, kind="log", status=200, duration_ms=40))
        ts += 0.25
    # then 30 seconds of pain
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, kind="log", status=200, duration_ms=1800))
        ts += 0.25

    anomalies = d.evaluate(now)
    assert anomalies
    anomaly = anomalies[0]
    assert any(s.name == "latency_ms" for s in anomaly.signals)
    assert anomaly.signals[0].zscore > 3


def test_cooldown_suppresses_duplicate_pages():
    d = AnomalyDetector()
    now = time.time()
    ts = now - settings.baseline_seconds
    while ts < now - settings.window_seconds:
        d.observe(TelemetryEvent(ts=ts, kind="log", status=200, duration_ms=40))
        ts += 0.25
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, kind="log", status=200, duration_ms=1800))
        ts += 0.25

    assert d.evaluate(now)
    assert d.evaluate(now + 1) == [], "fired twice inside the cooldown window"


def test_monotonic_memory_growth_is_caught_without_a_spike():
    d = AnomalyDetector()
    now = time.time()
    ts = now - settings.baseline_seconds
    value = 100.0
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, kind="metric", name="memory_rss_mb", value=value))
        value += 0.6  # gentle, monotonic - never looks like a spike
        ts += 2.0

    anomalies = d.evaluate(now)
    assert anomalies
    assert any(s.name == "memory_rss_mb" for s in anomalies[0].signals)


def test_single_outlier_does_not_page():
    """One 8-second GC pause in a healthy window is not an incident."""
    d = AnomalyDetector()
    now = time.time()
    ts = now - settings.baseline_seconds
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, kind="log", status=200, duration_ms=40))
        ts += 0.25
    d.observe(TelemetryEvent(ts=now - 1, kind="log", status=200, duration_ms=8000))
    assert d.evaluate(now) == []


def test_two_services_produce_two_separate_anomalies():
    """A sample-service latency spike and an AWS latency spike must not be
    merged into one incident - each source gets its own, correctly labelled."""
    d = AnomalyDetector()
    now = time.time()
    ts = now - settings.baseline_seconds
    while ts < now - settings.window_seconds:
        d.observe(TelemetryEvent(ts=ts, service="sample-service", kind="metric",
                                 name="latency_ms:/api/orders", value=40))
        d.observe(TelemetryEvent(ts=ts, service="aws:us-east-1", kind="metric",
                                 name="latency_ms:aws/prod-alb", value=30))
        ts += 0.5
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, service="sample-service", kind="metric",
                                 name="latency_ms:/api/orders", value=1800))
        d.observe(TelemetryEvent(ts=ts, service="aws:us-east-1", kind="metric",
                                 name="latency_ms:aws/prod-alb", value=990))
        ts += 0.5

    anomalies = d.evaluate(now)
    by_service = {a.service: a for a in anomalies}
    assert set(by_service) == {"sample-service", "aws:us-east-1"}
    # No cross-contamination: each anomaly carries only its own service's signal.
    assert all(s.name.startswith("latency_ms:aws/") for s in by_service["aws:us-east-1"].signals)
    assert all("aws/" not in s.name for s in by_service["sample-service"].signals)


def test_aws_error_logs_do_not_pollute_sample_service_error_rate():
    """A shared metric name (error_rate) is derived from every log line. If AWS
    error logs and the sample service shared one window, AWS errors would inflate
    the sample service's error_rate (and vice versa) and the metric would be
    owned by whichever source wrote last. Per-service windows keep them apart."""
    d = AnomalyDetector()
    now = time.time()
    ts = now - settings.baseline_seconds
    # Sample service stays perfectly healthy the whole time: only 200s, no errors.
    while ts < now:
        d.observe(TelemetryEvent(ts=ts, service="sample-service", kind="log",
                                 level="info", status=200, duration_ms=40))
        # AWS, meanwhile, is on fire: a burst of error logs in the recent window.
        level = "error" if ts >= now - settings.window_seconds else "info"
        d.observe(TelemetryEvent(ts=ts, service="aws:us-east-1", kind="log",
                                 level=level, message="upstream 502"))
        ts += 0.25

    anomalies = d.evaluate(now)
    # The sample service must NOT be dragged into an incident by AWS's errors.
    assert "sample-service" not in {a.service for a in anomalies}


def test_error_log_burst_opens_an_incident_without_a_baseline():
    """A read-only source (CloudWatch) gives sparse, backdated, baseline-less
    telemetry the statistical path can't use. A burst of error logs for that
    service must still open an incident - keyed on arrival, not event time, so
    an account that lags behind by minutes is still caught."""
    d = AnomalyDetector()
    now = time.time()
    # Six error lines whose event timestamps are two minutes stale (as a real
    # CloudWatch poll would deliver them), with no prior healthy baseline at all.
    for i in range(6):
        d.observe(TelemetryEvent(
            ts=now - 120 - i, service="aws:eu-north-1", kind="log",
            level="error", name="cloudwatch_logs",
            message="[ERROR] RuntimeError: simulated downstream failure (demo)",
        ))

    anomalies = d.evaluate(now)
    assert anomalies, "an error-log burst should open an incident on its own"
    aws = next(a for a in anomalies if a.service == "aws:eu-north-1")
    assert any(s.name == "error_logs" for s in aws.signals)


def test_a_couple_of_stray_errors_do_not_page():
    """Below the burst threshold, scattered errors are not an incident."""
    d = AnomalyDetector()
    now = time.time()
    for i in range(2):
        d.observe(TelemetryEvent(
            ts=now - i, service="aws:eu-north-1", kind="log",
            level="error", message="transient blip",
        ))
    assert d.evaluate(now) == []

