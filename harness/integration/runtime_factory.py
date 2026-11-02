"""Production composition root for the durable Mission -> Workflow Swarm."""

from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..lead.models import LeadRequest
from ..external_evidence import build_default_research_service
from ..mission import (
    JsonMissionStore,
    MissionNotFoundError,
    MissionSpecV1,
    WorkflowExecutionBridge as WorkflowBridge,
    MissionLifecyclePolicy as MissionPolicy,
)
from ..workflow import (
    AutonomousWorkflowController,
    ExternalContextSpine,
    WorkflowRecoveryBridge as WorkflowRecoveryBridge,
)
from ..repo_intelligence import FileRepositoryIndexStore, RepositoryContextBridge, RepositoryIntelligenceService
from ..scheduler import FileSchedulerStore, SchedulerCoordinator, SchedulerPolicy
from ..verification import VerificationCoordinator
from .common_runtime import CommonRuntimeComponents, create_common_runtime_components
from .ornith_planner_adapter import OrnithPlanningAdapter
from .planning_trace import PlanningTraceRecorder
from .traced_mission_controller import TracedDurableMissionController
from .mission_trace_observer import MissionTraceObserver, MissionTraceRegistry, TracingContextResolver
from .protocol_transport import constrain_recovery_bridge as constrain_recovery_bridge
from .role_invocation_tracing import (
    TracingCriticCoordinator,
    TracingJudgeCoordinator,
    TracingRecoveryBridge as TracingRecoveryBridge,
)
from .recovery_policy import (
    ProductionRecoveryBridge,
    production_workflow_policy,
)
from .failure_policy import DeterministicProductionRecoveryBridge
from .context_manifest import ProductionContextSpine
from .workflow_production_adapters import (
    ContextProfileRouterAdapter,
    NoDagAmendmentAdapter,
    ProductionCompletionAdapter,
    ProductionWorkspaceCoordinator,
    VerificationTodoOverlayRunStore,
    WorkflowExecutionAdapter,
    WorkflowFinalSynthesisAdapter,
    WorkflowRecoveryAdapter,
    WorkflowRepositoryAdapter,
    WorkflowReviewAdapter,
    WorkflowSchedulerAdapter,
    WorkflowSnapshotAdapter,
    WorkflowVerificationAdapter,
)
from .workflow_production_adapters import JudgeReplanAmendmentAdapter
from .research_workflow import (
    ConditionalResearchWorkflowBridge,
    bind_production_research_runtime,
)
from .work_item_registry import DurableWorkItemRegistry
from .execution_slices import ProductionExecutionSlicePolicy
from .verification_runtime import ProductionVerificationRunner


# Historical module-level compatibility symbol.
# Production composition uses WorkflowBridge internally.
WorkflowExecutionBridge = WorkflowBridge


@dataclass(slots=True)
class ProductionPortOverrides:
    """Offline test seam; production callers use concrete defaults."""

    planner: Any
    scheduler: Any
    repository: Any
    router: Any
    executor: Any
    verifier: Any
    reviewer: Any
    recovery: Any
    snapshots: Any
    final_synthesis: Any
    amendments: Any
    completion: Any
    spine: ExternalContextSpine
    workflow_wrapper: Callable[[WorkflowBridge], Any] | None = None


@dataclass(slots=True)
class SwarmRuntime:
    mission_controller: Any
    profile: str = "injected"
    mission_store: JsonMissionStore | None = None
    workflow_controller: AutonomousWorkflowController | Any | None = None
    workflow_bridge: WorkflowBridge | None = None
    scheduler: Any = None
    external_context_spine: ExternalContextSpine | None = None
    common: CommonRuntimeComponents | None = None
    trace_registry: MissionTraceRegistry | None = None
    execution_orchestrator: Any = None
    router: Any = None
    executor: Any = None


    def run_or_resume(self, spec: MissionSpecV1):
        if self.mission_store is None:
            raise RuntimeError("injected SwarmRuntime has no durable mission store")
        try:
            checkpoint = self.mission_store.load(spec.mission_id)
        except MissionNotFoundError:
            self.mission_controller.create(spec)
        else:
            if checkpoint.spec.to_dict() != spec.to_dict():
                raise ValueError("existing mission specification does not match requested mission")
        return self.mission_controller.run(spec.mission_id)

def create_swarm_runtime(
    *,
    mission_controller,
    workflow_controller=None,
    execution_orchestrator=None,
    router=None,
    executor=None,
):
    """Backward-compatible container used by earlier integration tests."""

    return SwarmRuntime(
        mission_controller=mission_controller,
        workflow_controller=workflow_controller,
        execution_orchestrator=execution_orchestrator,
        router=router,
        executor=executor,
    )


def _scheduler_policy(policies: dict[str, Any]) -> SchedulerPolicy:
    raw = policies.get("scheduler", {})
    return SchedulerPolicy(
        max_parallel_todos=int(raw.get("max_parallel_todos", 2)),
        max_active_claims=int(raw.get("max_active_claims", 8)),
        max_attempts_per_todo=int(raw.get("max_attempts_per_todo", 6)),
        claim_ttl_seconds=int(raw.get("claim_ttl_seconds", 900)),
        resource_lease_ttl_seconds=int(raw.get("resource_lease_ttl_seconds", 900)),
        lock_timeout_seconds=int(raw.get("lock_timeout_seconds", 10)),
        serialize_unknown_working_sets=bool(raw.get("serialize_unknown_working_sets", True)),
        require_judge_report=bool(raw.get("require_judge_report", True)),
        allow_parallel_conflict_free=bool(raw.get("allow_parallel_conflict_free", True)),
    )


def _required_gates(workspace: Path) -> tuple[str, ...]:
    gates = ["candidate_diff", "scope_guard"]
    if any(workspace.rglob("*.py")):
        gates.append("python_syntax")
    if (workspace / "tests").exists() or (workspace / "pytest.ini").exists():
        gates.append("pytest")
    return tuple(gates)


def _request_factory(workspace: Path):
    gates = _required_gates(workspace)

    def build(spec: MissionSpecV1) -> LeadRequest:
        constraints = dict(spec.global_constraints)
        from harness.scenarios.scope_evidence import SCOPE_REQUIREMENTS, SCOPE_EVIDENCE, PlanningScopeEvidenceV1
        if SCOPE_REQUIREMENTS in constraints:
            constraints[SCOPE_EVIDENCE] = PlanningScopeEvidenceV1.collect(spec, workspace).to_dict()
        constraints.update({
            "SEQUENTIAL_MODEL_EXECUTION": True,
            "ISOLATED_WORKTREE_REQUIRED": True,
            "NO_AUTO_MERGE": True,
            "verification_required_gates": list(gates),
        })
        return LeadRequest(
            objective=spec.raw_objective,
            constraints=constraints,
            forbidden_scope=(".git", "state"),
            metadata={"mission_id": spec.mission_id, "production_phase17_phase16": True,
                      **({"scope_workspace": str(workspace)} if SCOPE_REQUIREMENTS in constraints else {})},
        )

    return build


def _compose_from_ports(
    *,
    swarm_root: Path,
    state_root: Path,
    overrides: ProductionPortOverrides,
) -> SwarmRuntime:
    workflow_policy = production_workflow_policy(swarm_root)
    workflow = AutonomousWorkflowController(
        policy=workflow_policy,
        spine=overrides.spine,
        scheduler=overrides.scheduler,
        repository=overrides.repository,
        router=overrides.router,
        executor=overrides.executor,
        verifier=overrides.verifier,
        reviewer=overrides.reviewer,
        recovery=overrides.recovery,
        snapshots=overrides.snapshots,
        final_synthesis=overrides.final_synthesis,
    )
    workflow_bridge = WorkflowBridge(workflow)
    workflow_port = (
        overrides.workflow_wrapper(workflow_bridge)
        if overrides.workflow_wrapper
        else workflow_bridge
    )
    store = JsonMissionStore(state_root / "missions")
    mission = TracedDurableMissionController(trace_state_root=state_root,
        policy=MissionPolicy.from_project_root(swarm_root),
        store=store,
        planner=overrides.planner,
        workflow=workflow_port,
        amendments=overrides.amendments,
        completion=overrides.completion,
    )
    return SwarmRuntime(
        profile="production",
        mission_controller=mission,
        mission_store=store,
        workflow_controller=workflow,
        workflow_bridge=workflow_bridge,
        scheduler=overrides.scheduler,
        external_context_spine=overrides.spine,
    )


def create_production_verification_coordinator(
    *,
    run_store,
    artifact_store,
    manager,
    policy,
) -> VerificationCoordinator:
    coordinator = VerificationCoordinator(
        run_store=run_store,
        artifact_store=artifact_store,
        manager=manager,
        policy=policy,
    )
    coordinator.runner = ProductionVerificationRunner(
        manager=manager,
        policy=policy,
    )
    return coordinator


@legacy_keywords({'require_phase20v_environment': 'require_model_runtime_environment'})
def create_production_swarm_runtime(
    *,
    swarm_root: Path,
    workspace: Path,
    state_root: Path,
    port_overrides: ProductionPortOverrides | None = None,
    require_model_runtime_environment: bool = True,
) -> SwarmRuntime:
    """Build the authoritative production mission graph from existing components."""

    swarm_root = Path(swarm_root).resolve(strict=True)
    workspace = Path(workspace).resolve(strict=True)
    state_root = Path(state_root).resolve(strict=False)
    if port_overrides is not None:
        return _compose_from_ports(
            swarm_root=swarm_root,
            state_root=state_root,
            overrides=port_overrides,
        )

    common = create_common_runtime_components(
        swarm_root=swarm_root,
        workspace=workspace,
        state_root=state_root,
        require_model_runtime_environment=require_model_runtime_environment,
    )
    workflow_policy = production_workflow_policy(swarm_root)
    trace_registry = MissionTraceRegistry(state_root)
    trace_observer = MissionTraceObserver(trace_registry, strict=True)
    common.agent.context_resolver = TracingContextResolver(common.agent.context_resolver, trace_observer)
    common.agent.loop.observer = trace_observer
    common.critic = TracingCriticCoordinator(common.critic, trace_registry)
    common.judge = TracingJudgeCoordinator(common.judge, trace_registry)
    from .role_invocation_tracing import PublicInterpreterTraceObserver
    common.public_interpreter.observer = PublicInterpreterTraceObserver(trace_registry)
    spine = ProductionContextSpine(ExternalContextSpine(common.artifacts, workflow_policy))
    execution_slice_policy = ProductionExecutionSlicePolicy()
    registry = DurableWorkItemRegistry(common.artifacts, execution_slice_policy)
    workspace_coordinator = ProductionWorkspaceCoordinator(
        acceptance_interpreter=common.public_interpreter.interpret_public_requirements,
        run_store=common.run_store,
        manager=common.worktrees,
        source_repo_root=workspace,
        spine=spine,
        artifact_store=common.artifacts,
        execution_slice_policy=execution_slice_policy,
        trace_registry=trace_registry,
    )
    # Production has exactly one workspace authority. The scheduler allocates
    # the authoritative candidate lease from the durable project snapshot
    # (which may intentionally be a descendant research commit rather than
    # source HEAD). Reusing the legacy ControlPlaneIsolatedWorkspaceResolver
    # inside the agent would acquire the same Run/Todo again with its default
    # HEAD base and conflict as soon as research repair rebases onto a
    # descendant candidate. Bind the agent to the scheduler-owned coordinator
    # so execution resolves the already-authorized lease instead of allocating
    # a second one.
    common.agent.workspace_resolver = workspace_coordinator
    scheduler_coordinator = SchedulerCoordinator(
        store=FileSchedulerStore(state_root / "scheduler"),
        policy=_scheduler_policy(common.policies),
        run_store=common.run_store,
    )
    verification_run_store = VerificationTodoOverlayRunStore(common.run_store)
    scheduler = WorkflowSchedulerAdapter(
        coordinator=scheduler_coordinator,
        registry=registry,
        workspace=workspace_coordinator,
        todo_projection=verification_run_store.project,
    )
    planner = OrnithPlanningAdapter(
        common.lead,
        _request_factory(workspace),
        plan_registry=scheduler,
        required_gates=_required_gates(workspace),
        planning_trace=PlanningTraceRecorder(
            state_root / "planning-trace",
            mission_trace_registry=trace_registry,
        ),
    )
    repository_service = RepositoryIntelligenceService(
        manager=common.worktrees,
        index_store=FileRepositoryIndexStore(state_root / "repository-index"),
        artifact_store=common.artifacts,
    )
    repository = WorkflowRepositoryAdapter(
        service=repository_service,
        bridge=RepositoryContextBridge(manager=common.worktrees, artifact_store=common.artifacts),
        workspace=workspace_coordinator,
        spine=spine,
    )
    verification = create_production_verification_coordinator(
        run_store=verification_run_store,
        artifact_store=common.artifacts,
        manager=common.worktrees,
        policy=common.verification_policy,
    )
    workflow = AutonomousWorkflowController(
        policy=workflow_policy,
        spine=spine,
        scheduler=scheduler,
        repository=repository,
        router=ContextProfileRouterAdapter(workflow_policy),
        executor=WorkflowExecutionAdapter(
            agent=common.agent,
            workspace=workspace_coordinator,
            policy=workflow_policy,
            artifact_store=common.artifacts,
            execution_slice_policy=execution_slice_policy,
            trace_registry=trace_registry,
        ),
        verifier=WorkflowVerificationAdapter(coordinator=verification, workspace=workspace_coordinator, trace_registry=trace_registry),
        reviewer=WorkflowReviewAdapter(critic=common.critic, judge=common.judge, workspace=workspace_coordinator, trace_registry=trace_registry),
        recovery=WorkflowRecoveryAdapter(
            bridge=DeterministicProductionRecoveryBridge(
                bridge=ProductionRecoveryBridge(
                    TracingRecoveryBridge(
                        constrain_recovery_bridge(WorkflowRecoveryBridge(swarm_root)),
                        trace_registry,
                    ),
                    artifact_store=common.artifacts,
                ), artifact_store=common.artifacts,
                max_context_expansions=workflow_policy.max_context_expansions_per_todo,
            ),
            critic=common.critic,
            workspace=workspace_coordinator,
            trace_registry=trace_registry,
        ),
        snapshots=WorkflowSnapshotAdapter(source_repo_root=workspace, workspace=workspace_coordinator),
        final_synthesis=WorkflowFinalSynthesisAdapter(spine),
    )
    workflow_bridge = WorkflowBridge(workflow)
    workflow_port = ConditionalResearchWorkflowBridge(
        workflow_bridge=workflow_bridge,
        scheduler=scheduler,
        artifact_store=common.artifacts,
        service_factory=lambda: bind_production_research_runtime(
            build_default_research_service(
                swarm_root,
                state_root=state_root / "research",
            ),
            common.lead.runtime,
        ),
    )
    from .simple_worker_adapter import SimpleWorkerWorkflowAdapter
    from .simple_discovery_capability import SimpleDiscoveryCapability
    from harness.model_runtime import current_controller
    workflow.executor.typed_executor = SimpleWorkerWorkflowAdapter(
        legacy=workflow.executor, common=common, trace_registry=trace_registry,
        verification=verification, simple_native_mutation=True,
        discovery_capability=SimpleDiscoveryCapability(controller=current_controller(),
            registry=common.registry, executor=common.executor),
    )
    mission_store = JsonMissionStore(state_root / "missions")
    mission_controller = TracedDurableMissionController(trace_state_root=state_root, registry=trace_registry,
        policy=MissionPolicy.from_project_root(swarm_root),
        store=mission_store,
        planner=planner,
        workflow=workflow_port,
        amendments=JudgeReplanAmendmentAdapter(artifact_store=common.artifacts, scheduler=scheduler),
        completion=ProductionCompletionAdapter(spine=spine, scheduler=scheduler, verification_policy=common.verification_policy),
    )
    return SwarmRuntime(
        profile="production",
        mission_controller=mission_controller,
        mission_store=mission_store,
        workflow_controller=workflow,
        workflow_bridge=workflow_bridge,
        scheduler=scheduler,
        external_context_spine=spine,
        common=common,
        trace_registry=trace_registry,
    )

# Version-one constructor/attribute compatibility; wire keys stay byte-stable.
from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(SwarmRuntime, {'phase16_bridge': 'workflow_bridge'})

# Legacy import identity for previously public factory names.
Phase16WorkflowBridge = WorkflowExecutionBridge
Phase17Policy = MissionPolicy
