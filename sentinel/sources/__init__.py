"""Pluggable telemetry sources.

`sample_service` *pushes* telemetry to `POST /ingest`. Real systems - an AWS
account, a Jenkins server, a GitHub Actions repo - won't push to us, so a
*source* is the mirror image: it *pulls* from an external system on a timer and
normalises whatever it finds into `TelemetryEvent`s, feeding the exact same
`sentinel.ingest()` door the rest of the pipeline already listens on.

Adding a new integration is therefore just: implement `Source.run`, register it
in `build_sources`. Nothing downstream (detector, triage, diagnosis, memory)
changes, because everything speaks `TelemetryEvent`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

from ..config import settings
from ..models import TelemetryEvent

log = logging.getLogger("sentinel.sources")

# The callable a source uses to hand a normalised event to the pipeline.
Ingest = Callable[[TelemetryEvent], Awaitable[None]]


class Source(Protocol):
    name: str

    async def run(self, ingest: Ingest) -> None:
        """Poll forever, calling `ingest(event)` for every observation."""
        ...


def build_sources() -> list[Source]:
    """Instantiate every source enabled in config."""
    sources: list[Source] = []
    if settings.aws_enabled:
        from .aws import CloudWatchSource

        sources.append(CloudWatchSource())
    return sources


async def start_sources(ingest: Ingest) -> list[asyncio.Task]:
    """Launch each enabled source as its own supervised task."""
    tasks: list[asyncio.Task] = []
    for source in build_sources():
        tasks.append(asyncio.create_task(_supervise(source, ingest)))
        log.info("Source connected: %s", source.name)
    return tasks


async def _supervise(source: Source, ingest: Ingest) -> None:
    """A crashing source must never take the process down with it."""
    try:
        await source.run(ingest)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Source %s crashed", source.name)
