"""A tiny in-process pub/sub so the pipeline can stay ignorant of the UI.

The detector, reasoner and executor publish; the dashboard's websocket handler
subscribes. No extra infrastructure, and swapping in Redis later is a one-file
change.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import Any, AsyncIterator


class EventBus:
    def __init__(self, replay: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=replay)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    async def publish(self, type_: str, payload: Any = None, **fields: Any) -> None:
        event = {"type": type_, "payload": payload, **fields}
        self._history.append(event)
        for queue in list(self._subscribers):
            # A slow dashboard must never stall the agent.
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


bus = EventBus()
