#!/usr/bin/env python3
"""Break the sample service on purpose.

    python scripts/inject_fault.py --type memory_leak
    python scripts/inject_fault.py --type dependency_timeout
    python scripts/inject_fault.py --type n_plus_one
    python scripts/inject_fault.py --clear
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

SERVICE = os.getenv("SAMPLE_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
FAULTS = ("memory_leak", "dependency_timeout", "n_plus_one")

EXPECT = {
    "memory_leak": "memory_rss_mb will climb and never fall. Sentinel should catch the drift, not a spike.",
    "dependency_timeout": "latency_ms ramps for ~30s, then 5xx starts. Watch triage go from warning to critical.",
    "n_plus_one": "db_calls_per_request jumps from 1 to ~41 while latency creeps up.",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", choices=FAULTS, help="fault to inject")
    parser.add_argument("--clear", action="store_true", help="clear all injected faults")
    args = parser.parse_args()

    if not args.clear and not args.type:
        parser.error("pass --type or --clear")

    try:
        if args.clear:
            response = httpx.post(f"{SERVICE}/admin/clear_fault", json={"fault": "all"}, timeout=5)
            print("Cleared. Active faults:", response.json().get("active_faults"))
            return 0
        response = httpx.post(f"{SERVICE}/admin/inject", json={"fault": args.type}, timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Could not reach the sample service at {SERVICE}: {exc}", file=sys.stderr)
        print("Start it with: python sample_service/app.py", file=sys.stderr)
        return 1

    print(f"Injected {args.type}. Active faults: {response.json().get('active_faults')}")
    print(EXPECT[args.type])
    print("Open the dashboard and watch the incident stream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
