"""Shared concrete component builder for production and smoke profiles.

This module owns composition only.  Model selection, execution, verification,
worktree safety, and model-runtime lifecycle authority remain in their existing
subsystems.
"""

from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..agent_loop import CodingAgentLoop, ControlPlaneCodingAgent
from ..config import ConfigLoader
from ..context_manager import ControlPlaneContextResolver, FileArtifactStore
from ..control_plane import FileRunStore
from ..critic import CriticCoordinator, CriticPolicy
from ..executor import (
    LifecycleVerificationPolicy,
    OllamaTimeouts,
    WorkerExecutor,
    WorkerLifecycleManager,
)
from ..judge import JudgeCoordinator, JudgePolicy
from ..lead import OrnithLead, OrnithRuntime
from ..models import ModelRegistry
from ..model_runtime import install_runtime_orchestration
from ..resource_manager import ResourceManager
from ..router import ResourceAwareRouter
from ..runtime import discover_servers
from ..verification import VerificationPolicy, VerificationPublisher, VerificationRunner
from ..worktree import ControlPlaneIsolatedWorkspaceResolver, GitWorktreeManager, WorktreePolicy
from .protocol_transport import SchemaConstrainedOrnithRuntime
from .resource_policy import (
    ProductionResourceManager,
    load_worker_reserve,
    production_model_lifecycle_policy,
)
from .routing_audit import RoutingAuditProxy
from .single_endpoint import SingleEndpointOrnithRuntime
from .structured_worker_client import ProductionStructuredWorkerClient


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def worktree_policy(policies: dict[str, Any]) -> WorktreePolicy:
    raw = policies.get("worktree", {})
    if not isinstance(raw, dict):
        raise ValueError("policies.worktree must be a mapping")
    return WorktreePolicy(
        branch_prefix=str(raw.get("branch_prefix", "swarm/candidate")),
        require_clean_source=bool(raw.get("require_clean_source", True)),
        protect_same_git_common_dir=bool(raw.get("protect_same_git_common_dir", True)),
        allow_model_commands=bool(raw.get("allow_model_commands", False)),
        max_active_leases=int(raw.get("max_active_leases", 8)),
    )


@dataclass(slots=True)
class CommonRuntimeComponents:
    swarm_root: Path
    workspace: Path
    state_root: Path
    loader: ConfigLoader
    policies: dict[str, Any]
    registry: ModelRegistry
    resources: ResourceManager
    snapshot_provider: Callable[[], Any]
    router: ResourceAwareRouter
    executor: WorkerExecutor
    run_store: FileRunStore
    artifacts: FileArtifactStore
    worktrees: GitWorktreeManager
    workspace_resolver: ControlPlaneIsolatedWorkspaceResolver
    agent: ControlPlaneCodingAgent
    lead: OrnithLead
    verification_policy: VerificationPolicy
    verification_runner: VerificationRunner
    verification_publisher: VerificationPublisher
    critic: CriticCoordinator
    judge: JudgeCoordinator
    public_interpreter: Any = None


@legacy_keywords({'require_phase20v_environment': 'require_model_runtime_environment'})
def create_common_runtime_components(
    *,
    swarm_root: Path,
    workspace: Path,
    state_root: Path,
    require_model_runtime_environment: bool = True,
) -> CommonRuntimeComponents:
    """Build existing concrete subsystems once for either public profile."""

    swarm_root = Path(swarm_root).resolve(strict=True)
    workspace = Path(workspace).resolve(strict=True)
    state_root = Path(state_root).resolve(strict=False)
    if is_within(state_root, workspace):
        raise ValueError("state_root must be outside workspace")
    if require_model_runtime_environment and not os.environ.get("SWARM_MODEL_RUNTIME"):
        raise RuntimeError(
            "execution requires the production model-runtime launcher; use "
            "scripts/start_swarm_e2e.ps1 or the OpenCode swarm_run tool"
        )

    runtime_controller = install_runtime_orchestration(
        swarm_root,
        policy=production_model_lifecycle_policy(swarm_root),
    )
    runtime_controller.resolve_definition("deepseek_fast_coder")
    runtime_controller.resolve_definition("iquest_repo_coder")
    runtime_controller.refresh_registry()

    loader = ConfigLoader(swarm_root)
    policies = loader.load_policies()
    registry = ModelRegistry.from_loader(loader)
    resources = ProductionResourceManager(
        registry,
        loader.load_resources(),
        min_free_after_load_gb=load_worker_reserve(swarm_root),
        memory_policy=production_model_lifecycle_policy(swarm_root).memory_policy,
    )
    lead_url = registry.get_server("lead").ollama_base_url.rstrip("/")
    worker_url = registry.get_server("worker").ollama_base_url.rstrip("/")
    ornith_runtime_type = SingleEndpointOrnithRuntime if lead_url == worker_url else OrnithRuntime

    def snapshot_provider() -> Any:
        return resources.get_snapshot(discover_servers(registry))

    router = RoutingAuditProxy(
        ResourceAwareRouter(registry, resources, loader.load_routing(), policies),
        state_root / "routing-audit.jsonl",
    )
    client = ProductionStructuredWorkerClient(
        OllamaTimeouts.from_policies(policies)
    )
    lifecycle = WorkerLifecycleManager(
        registry,
        resources,
        client,
        snapshot_provider,
        LifecycleVerificationPolicy.from_policies(policies),
    )
    executor = WorkerExecutor(lifecycle, client, snapshot_provider)
    run_store = FileRunStore(state_root / "control-plane")
    artifacts = FileArtifactStore(state_root / "artifacts")
    worktrees = GitWorktreeManager(
        protected_repo_roots=(swarm_root,),
        managed_root=workspace.parent / ".swarm-worktrees",
        state_root=state_root / "worktrees",
        policy=worktree_policy(policies),
    )
    workspace_resolver = ControlPlaneIsolatedWorkspaceResolver(
        run_store,
        manager=worktrees,
        source_repo_root=workspace,
    )
    loop = CodingAgentLoop(
        run_store=run_store,
        artifact_store=artifacts,
        router=router,
        executor=executor,
        snapshot_provider=snapshot_provider,
        policies=policies,
    )
    agent = ControlPlaneCodingAgent(
        context_resolver=ControlPlaneContextResolver(run_store, artifacts),
        workspace_resolver=workspace_resolver,
        loop=loop,
    )
    lead_config = loader.load_lead()
    lead_runtime = ornith_runtime_type(
        registry,
        resources.locks,
        timeout_seconds=float(lead_config["lead"]["timeout_seconds"]),
        output_tokens=int(lead_config["lead"]["output_tokens"]),
    )
    verification_policy = VerificationPolicy.from_policies(policies, project_root=swarm_root)
    verification_runner = VerificationRunner(manager=worktrees, policy=verification_policy)
    verification_publisher = VerificationPublisher(run_store=run_store, artifact_store=artifacts)
    critic = CriticCoordinator(
        run_store=run_store,
        artifact_store=artifacts,
        manager=worktrees,
        router=router,
        executor=executor,
        snapshot_provider=snapshot_provider,
        policy=CriticPolicy.from_policies(policies),
    )
    from ..verification.public_witness import PublicWitnessRunner
    judge = JudgeCoordinator(
        public_witness_port=PublicWitnessRunner(verification_runner),
        require_acceptance=True,
        run_store=run_store,
        artifact_store=artifacts,
        manager=worktrees,
        runtime=SchemaConstrainedOrnithRuntime(
            ornith_runtime_type(
                registry,
                resources.locks,
                timeout_seconds=float(policies["judge"]["timeout_seconds"]),
                output_tokens=int(policies["judge"]["output_tokens"]),
            )
        ),
        policy=JudgePolicy.from_policies(policies),
    )
    from ..judge.public_interpreter import PublicInterpreterCoordinator
    from .public_interpreter_runtime import ProductionPublicInterpreterRuntime
    public_interpreter = PublicInterpreterCoordinator(
        artifact_store=artifacts, runner=verification_runner,
        runtime=ProductionPublicInterpreterRuntime(controller=runtime_controller,
            registry=registry, locks=resources.locks))
    return CommonRuntimeComponents(
        swarm_root=swarm_root,
        workspace=workspace,
        state_root=state_root,
        loader=loader,
        policies=policies,
        registry=registry,
        resources=resources,
        snapshot_provider=snapshot_provider,
        router=router,
        executor=executor,
        run_store=run_store,
        artifacts=artifacts,
        worktrees=worktrees,
        workspace_resolver=workspace_resolver,
        agent=agent,
        lead=OrnithLead(run_store, lead_runtime, lead_config, policies),
        verification_policy=verification_policy,
        verification_runner=verification_runner,
        verification_publisher=verification_publisher,
        critic=critic,
        judge=judge,
        public_interpreter=public_interpreter,
    )
