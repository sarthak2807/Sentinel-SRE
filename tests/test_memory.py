"""Retrieval quality, using the offline embedder so the test needs no network."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from sentinel.memory import IncidentMemory


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield IncidentMemory(Path(tmp) / "test.db")


def test_retrieves_the_semantically_closest_incident(store):
    async def run():
        await store.add({
            "id": "A", "title": "Memory grew until OOM",
            "symptoms": "RSS climbed steadily for hours, no spikes, latency normal",
            "root_cause": "unbounded cache", "resolution": "restart then bound the cache",
            "tags": ["memory_leak"],
        })
        await store.add({
            "id": "B", "title": "Auth upstream timing out",
            "symptoms": "latency rose then 503s appeared", "root_cause": "cold pool",
            "resolution": "lower timeout", "tags": ["dependency_timeout"],
        })
        hits = await store.search("memory rss climbing steadily and never released", k=1, min_score=0.0)
        assert hits and hits[0].incident_id == "A"

    asyncio.run(run())


def test_empty_store_returns_nothing(store):
    assert asyncio.run(store.search("anything")) == []
