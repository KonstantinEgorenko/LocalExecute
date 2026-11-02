"""Launch production Swarm under frozen guards on one physical Ollama endpoint."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from ..runtime_resources.gpu_coordination.guard import install_runtime_guard
from ..runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy
from ..gpu_acceleration.runtime import install_runtime_acceleration
from .resource_policy import production_model_lifecycle_policy
from ..model_runtime import install_runtime_orchestration
from .single_endpoint import install_single_endpoint_overlay, single_endpoint_gpu_acceleration_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--module", required=True)
    parser.add_argument("module_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.root.resolve()

    # This must precede every ConfigLoader/registry construction in the process.
    install_single_endpoint_overlay()
    gpu_policy = GpuCoordinationPolicy.from_project_root(root)
    ornith_policy = single_endpoint_gpu_acceleration_policy(root)
    runtime_policy = production_model_lifecycle_policy(root)
    os.environ[gpu_policy.enabled_env] = "1"
    os.environ[gpu_policy.root_env] = str(root)
    os.environ.setdefault(gpu_policy.lock_path_env, str(gpu_policy.lock_path()))
    os.environ[ornith_policy.enabled_env] = "1"
    os.environ[ornith_policy.root_env] = str(root)
    os.environ[runtime_policy.enabled_env] = "1"
    os.environ[runtime_policy.root_env] = str(root)
    os.environ.setdefault(runtime_policy.lock_path_env, str(runtime_policy.lock_path()))

    install_runtime_guard(root, policy=gpu_policy)
    install_runtime_acceleration(root, policy=ornith_policy)
    controller = install_runtime_orchestration(root, policy=runtime_policy)
    controller.reconcile_startup()
    profile_report = controller.ensure_worker_profiles(force_validate=False)
    if profile_report.get("blockers"):
        controller.journal.event(
            "production_launcher_worker_profile_blockers",
            blockers=profile_report["blockers"],
        )

    forwarded = list(args.module_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    sys.argv = [args.module, *forwarded]
    primary: BaseException | None = None
    try:
        runpy.run_module(args.module, run_name="__main__", alter_sys=False)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if runtime_policy.final_cleanup:
            try:
                controller.final_cleanup()
            except BaseException:
                if primary is None:
                    raise
                controller.journal.event("final_cleanup_failed_during_primary_exception")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
