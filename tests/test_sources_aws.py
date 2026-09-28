"""The AWS source turns CloudWatch responses into TelemetryEvents. Those
responses are external input, so the normalisation is what we test - with fake
boto3 clients, no network and no credentials."""
import asyncio
from datetime import datetime, timezone

from sentinel.models import TelemetryEvent
from sentinel.sources.aws import CloudWatchSource, MetricSpec, parse_metric_specs


def test_parse_metric_specs_reads_fields_and_dimensions():
    specs = parse_metric_specs(
        "AWS/ApplicationELB:TargetResponseTime:Average:prod-alb:latency_ms:LoadBalancer=app/prod;X=y,"
        "AWS/ApplicationELB:HTTPCode_Target_5XX_Count:Sum:prod-alb:http_5xx"
    )
    assert len(specs) == 2
    assert specs[0].namespace == "AWS/ApplicationELB"
    assert specs[0].metric == "TargetResponseTime"
    assert specs[0].stat == "Average"
    assert specs[0].as_name == "latency_ms"
    assert specs[0].dimensions == {"LoadBalancer": "app/prod", "X": "y"}
    # Namespaced so it never collides with the sample service's own latency_ms.
    assert specs[0].event_name == "latency_ms:aws/prod-alb"
    assert specs[1].dimensions == {}


def test_malformed_spec_is_skipped_not_crashed():
    specs = parse_metric_specs("too:few:fields, AWS/X:M:Average:lbl:latency_ms")
    assert len(specs) == 1
    assert specs[0].event_name == "latency_ms:aws/lbl"


class _FakeCloudWatch:
    def get_metric_data(self, **kwargs):
        return {
            "MetricDataResults": [
                {
                    "Id": "m0",
                    "Timestamps": [datetime(2026, 1, 1, tzinfo=timezone.utc)],
                    "Values": [812.5],
                }
            ]
        }


class _FakeLogs:
    def filter_log_events(self, **kwargs):
        return {
            "events": [
                {"timestamp": 1_700_000_000_000, "message": "ERROR upstream 502 from target"},
                {"timestamp": 1_700_000_001_000, "message": "WARN retry budget low"},
            ]
        }


def _drain(coro_fn):
    events: list[TelemetryEvent] = []

    async def ingest(ev):
        events.append(ev)

    asyncio.run(coro_fn(ingest))
    return events


def test_metrics_normalise_to_namespaced_metric_events(monkeypatch):
    src = CloudWatchSource()
    src.specs = parse_metric_specs("AWS/ApplicationELB:TargetResponseTime:Average:prod-alb:latency_ms")
    events = _drain(lambda ingest: src._poll_metrics(_FakeCloudWatch(), ingest))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "metric"
    assert ev.name == "latency_ms:aws/prod-alb"
    assert ev.value == 812.5
    assert ev.service.startswith("aws:")
    assert ev.attrs["source"] == "cloudwatch"


def test_error_logs_normalise_to_log_events():
    src = CloudWatchSource()
    src.log_groups = ["/aws/prod"]
    events = _drain(lambda ingest: src._poll_logs(_FakeLogs(), ingest))
    assert [e.kind for e in events] == ["log", "log"]
    assert events[0].level == "error"
    assert events[1].level == "warn"
    assert "502" in events[0].message
    # Watermark advanced so the same line is not re-ingested next poll.
    assert src._last_log_ts["/aws/prod"] == 1_700_000_001_000
