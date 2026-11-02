"model runtime child-process bootstrap for guarded Swarm Python processes.\n\nThis file is imported by Python only when Phase20V's bootstrap directory is\nprepended to PYTHONPATH by scripts/model_runtime_guarded_python.ps1.  It installs the\nsame gpu coordination -> gpu acceleration -> model runtime runtime guards in descendants without\nmodifying frozen root sitecustomize.py/usercustomize.py.\n"
from __future__ import annotations

import os
import sys
from pathlib import Path


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}


if _enabled("SWARM_MODEL_RUNTIME"):
    try:
        root_text = os.getenv("SWARM_MODEL_RUNTIME_ROOT", "").strip()
        if not root_text:
            raise RuntimeError("SWARM_MODEL_RUNTIME_ROOT is required")
        root = Path(root_text).resolve()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from harness.runtime_resources.gpu_coordination.guard import install_runtime_guard
        from harness.runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy
        from harness.gpu_acceleration.policy import GpuAccelerationPolicy
        from harness.gpu_acceleration.runtime import install_runtime_acceleration
        from harness.model_runtime.policy import ModelLifecyclePolicy
        from harness.model_runtime.runtime import install_runtime_orchestration

        g = GpuCoordinationPolicy.from_project_root(root)
        u = GpuAccelerationPolicy.from_project_root(root)
        v = ModelLifecyclePolicy.from_project_root(root)
        install_runtime_guard(root, policy=g)
        install_runtime_acceleration(root, policy=u)
        install_runtime_orchestration(root, policy=v)
    except BaseException as exc:
        sys.stderr.write(f"PHASE20V MODEL RUNTIME BOOTSTRAP FAILED: {exc}\n")
        sys.stderr.flush()
        os._exit(88)
