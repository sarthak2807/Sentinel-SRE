"""The service Sentinel watches - and that we break on purpose.

It emits structured telemetry to Sentinel's /ingest endpoint on every request
plus a 2-second metrics heartbeat. Real deployments would point an OTel
collector at the same endpoint; this keeps the demo to two processes.

Run:  python sample_service/app.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .faults import (
    apply_dependency_timeout,
    apply_memory_leak,
    apply_n_plus_one,
    memory_rss_mb,
    state,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("sample-service")

SENTINEL = os.getenv("SENTINEL_URL", "http://127.0.0.1:8080").rstrip("/")
SERVICE = "sample-service"

_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)


async def emit(**event) -> None:
    event.setdefault("ts", time.time())
    event.setdefault("service", SERVICE)
    with contextlib.suppress(asyncio.QueueFull):
        _queue.put_nowait(event)


async def _shipper() -> None:
    """Batch telemetry out of the request path so instrumentation never blocks it."""
    async with httpx.AsyncClient(timeout=3.0) as http:
        while True:
            batch = [await _queue.get()]
            while len(batch) < 50:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            with contextlib.suppress(Exception):
                await http.post(f"{SENTINEL}/ingest", json=batch)


async def _heartbeat() -> None:
    while True:
        await emit(kind="metric", name="memory_rss_mb", value=memory_rss_mb())
        await emit(kind="metric", name="active_faults", value=float(len(state.active)))
        await asyncio.sleep(2)


async def _synthetic_traffic() -> None:
    """Keep a baseline flowing so the detector has something to be surprised by."""
    await asyncio.sleep(1)
    async with httpx.AsyncClient(timeout=15.0, base_url="http://127.0.0.1:8000") as http:
        while True:
            with contextlib.suppress(Exception):
                await http.get(random.choice(["/api/orders", "/api/users", "/api/search"]))
            await asyncio.sleep(random.uniform(0.15, 0.4))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_shipper()),
        asyncio.create_task(_heartbeat()),
        asyncio.create_task(_synthetic_traffic()),
    ]
    log.info("Sample service up on :8000, reporting to %s", SENTINEL)
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Sample Service", lifespan=lifespan)


@app.middleware("http")
async def instrument(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        if not request.url.path.startswith("/admin"):
            await emit(
                kind="log",
                name="http_request",
                level="error" if status >= 500 else "info",
                route=request.url.path,
                status=status,
                duration_ms=round(duration_ms, 2),
                message=f"{request.method} {request.url.path} -> {status} in {duration_ms:.0f}ms",
            )


# ------------------------------------------------------------ business routes


@app.get("/api/orders")
async def orders():
    apply_memory_leak()
    queries = apply_n_plus_one(rows=40)
    await emit(kind="metric", name="db_calls_per_request", value=float(queries))
    await asyncio.sleep(0.004 * queries)
    return {"orders": 40, "queries": queries}


@app.get("/api/users")
async def users():
    apply_memory_leak()
    latency = await apply_dependency_timeout()
    if latency >= state.upstream_timeout_s:
        await emit(
            kind="log",
            level="error",
            name="upstream_timeout",
            message=f"auth-service call exceeded {state.upstream_timeout_s}s budget",
        )
        return JSONResponse({"error": "upstream timeout"}, status_code=503)
    return {"users": 12}


@app.get("/api/search")
async def search(q: str = "widget"):
    apply_memory_leak()
    await asyncio.sleep(random.uniform(0.02, 0.06))
    return {"q": q, "hits": random.randint(0, 25)}


# ----------------------------------------------------------------- admin API
# These are the endpoints Sentinel's allowlisted remediation actions call.


class ConfigBody(BaseModel):
    upstream_timeout_s: float | None = None
    cache_ttl_s: int | None = None
    workers: int | None = None


class FaultBody(BaseModel):
    fault: str = "all"


@app.get("/health")
async def health():
    return {
        "healthy": state.healthy,
        "active_faults": sorted(state.active),
        "memory_rss_mb": round(memory_rss_mb(), 1),
        "config": {
            "upstream_timeout_s": state.upstream_timeout_s,
            "cache_ttl_s": state.cache_ttl_s,
            "workers": state.workers,
        },
        "uptime_note": "restarting clears leaked memory but not the injected fault flag",
    }


@app.post("/admin/restart")
async def restart():
    state.restart()
    await emit(kind="log", level="warn", name="restart", message="worker restarted by Sentinel")
    return {"restarted": True, "memory_rss_mb": round(memory_rss_mb(), 1)}


@app.post("/admin/config")
async def configure(body: ConfigBody):
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(state, key, value)
        if key == "cache_ttl_s" and value:
            state.active.discard("n_plus_one")
        if key == "upstream_timeout_s":
            state.active.discard("dependency_timeout")
    await emit(kind="log", level="warn", name="config_change", message=f"config updated: {body}")
    return {"config": body.model_dump(exclude_none=True), "active_faults": sorted(state.active)}


@app.post("/admin/inject")
async def inject(body: FaultBody):
    state.enable(body.fault)
    await emit(kind="log", level="warn", name="fault_injected", message=f"fault injected: {body.fault}")
    return {"active_faults": sorted(state.active)}


@app.post("/admin/clear_fault")
async def clear_fault(body: FaultBody):
    state.clear(body.fault)
    return {"active_faults": sorted(state.active)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
