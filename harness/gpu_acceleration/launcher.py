'Run an arbitrary Python module with gpu coordination + gpu acceleration installed first.'
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from harness.runtime_resources.gpu_coordination.guard import install_runtime_guard
from harness.runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy

from .policy import GpuAccelerationPolicy
from .runtime import install_runtime_acceleration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one module under GPU_HEAVY + Ornith GPU burst acceleration.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--module", required=True)
    parser.add_argument("module_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.root.resolve()

    g = GpuCoordinationPolicy.from_project_root(root)
    u = GpuAccelerationPolicy.from_project_root(root)
    os.environ[g.enabled_env] = "1"
    os.environ[g.root_env] = str(root)
    os.environ.setdefault(g.lock_path_env, str(g.lock_path()))
    os.environ[u.enabled_env] = "1"
    os.environ[u.root_env] = str(root)

    install_runtime_guard(root, policy=g)
    install_runtime_acceleration(root, policy=u)

    forwarded = list(args.module_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    sys.argv = [args.module, *forwarded]
    runpy.run_module(args.module, run_name="__main__", alter_sys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
