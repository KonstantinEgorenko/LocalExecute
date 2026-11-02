'Launch arbitrary Swarm module under gpu coordination + gpu acceleration + model runtime.'
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from harness.runtime_resources.gpu_coordination.guard import install_runtime_guard
from harness.runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy
from harness.gpu_acceleration.policy import GpuAccelerationPolicy
from harness.gpu_acceleration.runtime import install_runtime_acceleration

from .policy import ModelLifecyclePolicy
from .runtime import install_runtime_orchestration


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--module", required=True)
    p.add_argument("module_args", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    root = args.root.resolve()

    g = GpuCoordinationPolicy.from_project_root(root)
    u = GpuAccelerationPolicy.from_project_root(root)
    v = ModelLifecyclePolicy.from_project_root(root)
    os.environ[g.enabled_env] = "1"; os.environ[g.root_env] = str(root)
    os.environ.setdefault(g.lock_path_env, str(g.lock_path()))
    os.environ[u.enabled_env] = "1"; os.environ[u.root_env] = str(root)
    os.environ[v.enabled_env] = "1"; os.environ[v.root_env] = str(root)
    os.environ.setdefault(v.lock_path_env, str(v.lock_path()))

    install_runtime_guard(root, policy=g)
    install_runtime_acceleration(root, policy=u)
    controller = install_runtime_orchestration(root, policy=v)
    controller.reconcile_startup()
    profile_report = controller.ensure_worker_profiles(force_validate=False)
    if profile_report.get("blockers"):
        controller.journal.event("launcher_worker_profile_blockers", blockers=profile_report["blockers"])

    forwarded = list(args.module_args)
    if forwarded and forwarded[0] == "--": forwarded = forwarded[1:]
    sys.argv = [args.module, *forwarded]
    primary: BaseException | None = None
    try:
        runpy.run_module(args.module, run_name="__main__", alter_sys=False)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if v.final_cleanup:
            try:
                controller.final_cleanup()
            except BaseException:
                if primary is None:
                    raise
                controller.journal.event("final_cleanup_failed_during_primary_exception")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
