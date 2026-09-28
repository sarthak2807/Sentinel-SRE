"""Incident memory: RAG over everything Sentinel has seen before.

Why SQLite + numpy instead of Chroma/Qdrant: at hackathon scale the whole corpus
is a few hundred incidents. A vector DB would add a container, a client library
and a failure mode to the demo without changing a single retrieval result. The
interface below is narrow enough (`add`, `search`) that swapping in Qdrant later
is one file.

The important part is not the store, it is that Sentinel *writes back*: every
resolved incident becomes a document, so the second time a fault appears the
agent recognises it and its confidence goes up.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from .config import settings
from .models import Anomaly, Incident, MemoryHit

log = logging.getLogger("sentinel.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    occurred_at  TEXT,
    service      TEXT,
    symptoms     TEXT,
    root_cause   TEXT,
    resolution   TEXT,
    tags         TEXT,
    document     TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    created_at   REAL NOT NULL
);
"""


def _to_document(record: dict[str, Any]) -> str:
    """One flat string per incident - this is what actually gets embedded."""
    parts = [
        record.get("title", ""),
        f"service: {record.get('service', '')}",
        f"symptoms: {record.get('symptoms', '')}",
        f"root cause: {record.get('root_cause', '')}",
        f"resolution: {record.get('resolution', '')}",
        f"tags: {', '.join(record.get('tags', []) or [])}",
    ]
    return "\n".join(p for p in parts if p.strip())


class IncidentMemory:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ write

    async def add(self, record: dict[str, Any]) -> str:
        from .nemotron import client

        document = _to_document(record)
        vector = (await client.embed([document]))[0]
        incident_id = record.get("id") or f"mem_{int(time.time() * 1000)}"
        self.conn.execute(
            "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                incident_id,
                record.get("title", "untitled"),
                record.get("occurred_at", ""),
                record.get("service", ""),
                record.get("symptoms", ""),
                record.get("root_cause", ""),
                record.get("resolution", ""),
                json.dumps(record.get("tags", [])),
                document,
                np.asarray(vector, dtype=np.float32).tobytes(),
                time.time(),
            ),
        )
        self.conn.commit()
        return incident_id

    async def remember_resolved(self, incident: Incident) -> str | None:
        """Close the loop: a handled incident becomes tomorrow's context."""
        if not incident.diagnosis:
            return None
        symptoms = "; ".join(
            f"{s.name} {s.observed}{s.unit} vs {s.baseline}{s.unit}"
            for s in (incident.anomaly.signals if incident.anomaly else [])
        )
        fix = incident.diagnosis.proposed_fix
        return await self.add(
            {
                "id": incident.id,
                "title": incident.title or (incident.anomaly.title if incident.anomaly else "incident"),
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(incident.opened_at)),
                "service": incident.service,
                "symptoms": symptoms,
                "root_cause": incident.diagnosis.root_cause,
                "resolution": (
                    f"{fix.action}({json.dumps(fix.params)}) - {fix.expected_effect}" if fix else "no fix applied"
                ),
                "tags": [incident.triage.category] if incident.triage else [],
            }
        )

    # ------------------------------------------------------------------- read

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]

    async def search(self, query: str, k: int = 3, min_score: float = 0.25) -> list[MemoryHit]:
        rows = self.conn.execute("SELECT * FROM incidents").fetchall()
        if not rows:
            return []
        from .nemotron import client

        query_vector = np.asarray((await client.embed([query]))[0], dtype=np.float32)
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        if matrix.shape[1] != query_vector.shape[0]:
            log.warning(
                "Embedding dimension changed (%s stored vs %s live) - rebuild the store "
                "with scripts/setup_vector_store.py",
                matrix.shape[1],
                query_vector.shape[0],
            )
            return []

        norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vector)
        norms[norms == 0] = 1e-9
        scores = matrix @ query_vector / norms

        order = np.argsort(-scores)[:k]
        return [
            MemoryHit(
                incident_id=rows[i]["id"],
                title=rows[i]["title"],
                score=round(float(scores[i]), 3),
                root_cause=rows[i]["root_cause"] or "",
                resolution=rows[i]["resolution"] or "",
                occurred_at=rows[i]["occurred_at"] or "",
            )
            for i in order
            if scores[i] >= min_score
        ]

    async def search_for_anomaly(self, anomaly: Anomaly, k: int = 3) -> list[MemoryHit]:
        query = f"{anomaly.title}. " + "; ".join(
            f"{s.name} at {s.observed}{s.unit}, normally {s.baseline}{s.unit}" for s in anomaly.signals
        )
        return await self.search(query, k=k)


memory = IncidentMemory()
