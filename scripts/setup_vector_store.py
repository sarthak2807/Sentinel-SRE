#!/usr/bin/env python3
"""Load seed incidents into Sentinel's memory. Run once before the demo.

    python scripts/setup_vector_store.py
    python scripts/setup_vector_store.py --reset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentinel.memory import memory  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop existing rows first")
    parser.add_argument("--file", default=str(ROOT / "incidents" / "seed_incidents.json"))
    args = parser.parse_args()

    if args.reset:
        memory.conn.execute("DELETE FROM incidents")
        memory.conn.commit()
        print("Cleared existing incident memory.")

    records = json.loads(Path(args.file).read_text(encoding="utf-8"))
    for record in records:
        incident_id = await memory.add(record)
        print(f"  + {incident_id}  {record['title']}")

    print(f"\nIncident memory now holds {memory.count()} incidents.")
    hits = await memory.search("memory usage keeps climbing and never comes back down")
    print("\nSanity check - retrieval for a memory-growth query:")
    for hit in hits:
        print(f"  {hit.score:>5}  {hit.title}")


if __name__ == "__main__":
    asyncio.run(main())