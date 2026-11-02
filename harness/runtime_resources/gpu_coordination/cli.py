"""Small operational CLI for inspecting/testing the shared GPU_HEAVY lease."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .lease import GpuHeavyLease, GpuHeavyLeaseTimeout
from .policy import GpuCoordinationPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or exercise the Phase20G GPU_HEAVY lease.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    hold = sub.add_parser("hold")
    hold.add_argument("--owner", default="manual-phase20g-test")
    hold.add_argument("--seconds", type=float, default=10.0)
    hold.add_argument("--timeout", type=float)
    args = parser.parse_args()
    policy = GpuCoordinationPolicy.from_project_root(args.root.resolve())
    lease = GpuHeavyLease(policy.lock_path(), poll_interval_seconds=policy.poll_interval_seconds)
    if args.command == "status":
        print(json.dumps(asdict(lease.status()), indent=2, ensure_ascii=False))
        return 0
    timeout = policy.acquire_timeout_seconds if args.timeout is None else args.timeout
    try:
        with lease.held(args.owner, timeout_seconds=timeout):
            print(json.dumps(asdict(lease.status()), indent=2, ensure_ascii=False), flush=True)
            time.sleep(args.seconds)
    except GpuHeavyLeaseTimeout as exc:
        print(f"GPU_HEAVY_BUSY: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
