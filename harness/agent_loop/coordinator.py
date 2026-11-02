'High-level worker agent composition over context management context and workspace workspace.'
from __future__ import annotations

from collections.abc import Mapping

from ..context_manager.integration import ControlPlaneContextResolver
from ..context_manager.models import ArtifactRef, ContextBudget
from ..control_plane import WorkContract
from ..orchestrator.models import RoutingTaskSpec
from ..workspace.integration import ControlPlaneWorkspaceResolver
from .loop import CodingAgentLoop
from .models import AgentLoopResult


class ControlPlaneCodingAgent:
    """Assemble context + workspace, then execute one bounded CodingAgentLoop."""

    def __init__(
        self,
        *,
        context_resolver: ControlPlaneContextResolver,
        workspace_resolver: ControlPlaneWorkspaceResolver,
        loop: CodingAgentLoop,
    ) -> None:
        self.context_resolver = context_resolver
        self.workspace_resolver = workspace_resolver
        self.loop = loop

    def execute(
        self,
        *,
        run_id: str,
        contract: WorkContract,
        routing_spec: RoutingTaskSpec,
        budget: ContextBudget,
        dependency_run_ids: Mapping[str, str] | None = None,
        required_artifacts: tuple[ArtifactRef, ...] = (),
        optional_artifacts: tuple[ArtifactRef, ...] = (),
        request_id: str | None = None,
        task_id: str | None = None,
        keep_alive: int | str = -1,
        workspace_contract: WorkContract | None = None,
    ) -> AgentLoopResult:
        # Prompt context and slice accounting may decorate the worker contract.
        # Workspace authority remains bound to the admitted canonical contract.
        authorization = contract if workspace_contract is None else workspace_contract
        for field in ('goal_id', 'todo_id', 'constraints', 'allowed_scope', 'forbidden_scope', 'required_evidence'):
            if getattr(authorization, field) != getattr(contract, field):
                raise ValueError('worker prompt cannot change workspace authorization')
        bundle = self.context_resolver.assemble(
            run_id,
            contract,
            budget,
            dependency_run_ids=dependency_run_ids,
            required_artifacts=required_artifacts,
            optional_artifacts=optional_artifacts,
            request_id=f"{run_id}:{contract.todo_id}:agent-context",
        )
        session = self.workspace_resolver.resolve(run_id=run_id, contract=authorization)
        return self.loop.execute(
            run_id=run_id,
            contract=contract,
            routing_spec=routing_spec,
            context_bundle=bundle,
            workspace_session=session,
            request_id=request_id,
            task_id=task_id,
            keep_alive=keep_alive,
        )
