'Run an arbitrary Python module with the gpu coordination guard installed first.'
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from .guard import install_runtime_guard
from .policy import GpuCoordinationPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one module under the machine-wide GPU_HEAVY guard.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--module", required=True)
    parser.add_argument("module_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    policy = GpuCoordinationPolicy.from_project_root(root)
    os.environ[policy.enabled_env] = "1"
    os.environ[policy.root_env] = str(root)
    os.environ.setdefault(policy.lock_path_env, str(policy.lock_path()))
    install_runtime_guard(root, policy=policy)
    forwarded = list(args.module_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    sys.argv = [args.module, *forwarded]
    runpy.run_module(args.module, run_name="__main__", alter_sys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
