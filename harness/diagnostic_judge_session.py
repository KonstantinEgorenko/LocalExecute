"""Bounded diagnostic composition over the installed production runtime.

No Mission, scheduler, residency controller or implicit profile reconfiguration.
Admission, evidence persistence and physical dispatch remain caller responsibilities.
"""
from contextlib import contextmanager
from pathlib import Path

from harness.runtime_resources.gpu_coordination.guard import current_lease
from harness.gpu_acceleration.runtime import current_manager
from harness.model_runtime.runtime import current_controller, install_runtime_orchestration
from harness.runtime_hardening.transport import observe_physical_http


class DiagnosticJudgeSession:
    def __init__(self, root, *, runtime_policy=None):
        self.root = Path(root).resolve()
        self.manager = current_manager()
        self.lease = current_lease()
        if self.manager is None or self.lease is None:
            raise RuntimeError("DIAGNOSTIC_PRODUCTION_BOOTSTRAP_REQUIRED")
        if self.manager.root != self.root or self.manager.lease is not self.lease:
            raise RuntimeError("DIAGNOSTIC_RUNTIME_OWNER_MISMATCH")
        self.controller = current_controller()
        if self.controller is None:
            self.controller = install_runtime_orchestration(self.root, policy=runtime_policy)
        if self.controller.root != self.root or self.controller.gpu_lease is not self.lease:
            raise RuntimeError("DIAGNOSTIC_RUNTIME_OWNER_MISMATCH")
        if runtime_policy is not None and self.controller.policy != runtime_policy:
            raise RuntimeError("DIAGNOSTIC_INSTALLED_POLICY_MISMATCH")

    @contextmanager
    def active(self, owner, observer, *, admit):
        """admit validates exact residency/GPU/RAM under both authoritative locks.

        The observer must persist physical accounting before invoking dispatch.
        Production JudgePolicy and timeout hardening are left intact.
        """
        def gpu_wire(request, timeout, dispatch):
            import json
            from harness.lead import runtime as lead
            from harness.runtime_hardening import transport
            if lead.urlopen is not transport._WRAPPER:
                raise RuntimeError("PHASE20R_HARDENING_CHAIN_MISMATCH")
            if not self.lease.held_by_current_thread or not self.controller.runtime_lease.held_by_current_thread:
                raise RuntimeError("DIAGNOSTIC_OS_OWNERSHIP_MISSING")
            if not self.manager.gpu_active or json.loads(request.data).get('options', {}).get('num_gpu') != self.manager.policy.gpu_num_gpu:
                raise RuntimeError("DIAGNOSTIC_GPU_REQUIRED_WIRE_MISMATCH")
            return observer(request, timeout, dispatch)

        if current_manager() is not self.manager or current_lease() is not self.lease or current_controller() is not self.controller:
            raise RuntimeError("DIAGNOSTIC_RUNTIME_OWNER_MISMATCH")
        with self.manager.require_gpu(), observe_physical_http(gpu_wire):
            with self.controller.model_transaction(owner, "ornith_lead"):
                with self.manager.burst(owner) as placement:
                    if placement.mode != "gpu":
                        raise RuntimeError("DIAGNOSTIC_GPU_REQUIRED_PLACEMENT")
                    admit(self, placement)
                    yield self
