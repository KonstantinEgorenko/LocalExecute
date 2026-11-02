"""Refresh admission for one selected worker without selecting a new model."""
from dataclasses import replace

from harness.errors import StaleRoutingDecisionError
from harness.models import ModelStatus
from harness.orchestrator.profile_factory import TaskProfileFactory


class SelectedWorkerAdmissionExecutor:
    def __init__(self, *, executor, router, snapshot_provider, routing_spec):
        self.executor = executor
        self.router = getattr(router, "inner", router)
        self.snapshot_provider = snapshot_provider
        self.routing_spec = routing_spec

    def execute(self, request):
        selected = request.routing_decision.selected_model
        snapshot = self.snapshot_provider()
        profile = TaskProfileFactory().build(self.routing_spec, snapshot, ())
        status = next((s for s in snapshot.model_statuses if s.logical_id == selected), None)
        if status is None or status.status not in (ModelStatus.LOADED, ModelStatus.UNLOADED):
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected worker discovery unavailable")
        feasibility = self.router._feasibility(selected, profile, status)
        if feasibility is None:
            registry = getattr(self.router, "registry", None)
            if registry is None or registry.get(selected).runtime.get("execution") != "cpu":
                raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected worker admission unresolved")
            # Router intentionally has no GPU-worker feasibility for CPU
            # profiles. Their existing lifecycle/burst authority still owns
            # effective placement and locks; never manufacture GPU admission.
            admission = None
        elif not feasibility.allowed:
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected worker admission denied")
        else:
            admission = feasibility.to_dict()
        decision = replace(request.routing_decision, resource_admission=admission)
        # Lifecycle still revalidates under its locks. A race here fails closed.
        return self.executor.execute(replace(request, routing_decision=decision))
