"""Sentinel's single process: telemetry intake, agent loop, and the on-call UI.

The original plan ran the agent and the dashboard as two services. Folding them
into one removes an entire class of demo failure (ordering, ports, stale state)
and costs nothing - the dashboard is a read model over the same in-process bus
the pipeline publishes to.

Run:  python -m sentinel.server
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path


from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .bus import bus
from .config import settings
from .models import TelemetryEvent
from .pipeline import sentinel
from .sources import start_sources

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("sentinel.server")

WEB = Path(__file__).resolve().parent / "web"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await sentinel.start()
    source_tasks = await start_sources(sentinel.ingest)
    log.info(
        "Sentinel up on :%s | Nemotron %s | sandbox=%s | auto_approve=%s | sources=%d",
        settings.port,
        "online" if settings.online else "OFFLINE STUB (set NEBIUS_API_KEY)",
        settings.sandbox_mode,
        settings.auto_approve,
        len(source_tasks),
    )
    yield
    for task in source_tasks:
        task.cancel()
    await sentinel.stop()


app = FastAPI(title="Sentinel", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ telemetry


@app.post("/ingest")
async def ingest(events: list[TelemetryEvent] | TelemetryEvent):
    batch = events if isinstance(events, list) else [events]
    for event in batch:
        await sentinel.ingest(event)
    return {"accepted": len(batch)}


# ------------------------------------------------------------------- read API


@app.get("/api/status")
async def status():
    return sentinel.status()


@app.get("/api/incidents")
async def incidents(limit: int = 20):
    return [i.model_dump() for i in sentinel.recent(limit)]


@app.get("/api/incidents/{incident_id}")
async def incident(incident_id: str):
    item = sentinel.incidents.get(incident_id)
    if not item:
        raise HTTPException(404, "No such incident")
    return item.model_dump()


@app.get("/api/series/{name}")
async def series(name: str, points: int = 120):
    return sentinel.series(name, points)


# ------------------------------------------------------------------ write API


class ApprovalBody(BaseModel):
    approved_by: str = "on-call"


class RejectBody(BaseModel):
    reason: str = ""


class AskBody(BaseModel):
    question: str
    incident_id: str | None = None


@app.post("/api/incidents/{incident_id}/approve")
async def approve(incident_id: str, body: ApprovalBody):
    if incident_id not in sentinel.incidents:
        raise HTTPException(404, "No such incident")
    result = await sentinel.approve(incident_id, approved_by=body.approved_by)
    return result.model_dump()


@app.post("/api/incidents/{incident_id}/reject")
async def reject(incident_id: str, body: RejectBody):
    if incident_id not in sentinel.incidents:
        raise HTTPException(404, "No such incident")
    result = await sentinel.reject(incident_id, reason=body.reason)
    return result.model_dump()


@app.post("/api/ask")
async def ask(body: AskBody):
    await bus.publish("chat", {"role": "you", "text": body.question})
    answer = await sentinel.ask(body.question, body.incident_id)
    await bus.publish("chat", {"role": "sentinel", "text": answer})
    return {"answer": answer}


# ------------------------------------------------------------------ live feed


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    async with bus.subscribe() as queue:
        await ws.send_text(json.dumps({"type": "hello", "payload": sentinel.status()}))
        for event in bus.history()[-40:]:
            await ws.send_text(json.dumps(event, default=str))
        try:
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event, default=str))
        except (WebSocketDisconnect, RuntimeError):
            return


# ---------------------------------------------------------------------- UI


@app.get("/")
async def index():
    page = WEB / "index.html"
    if not page.exists():
        return JSONResponse({"error": "dashboard not built"}, status_code=500)
    return FileResponse(page)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "sentinel.server:app", host="0.0.0.0", port=settings.port, log_level="info"
    )


if __name__ == "__main__":
    main()