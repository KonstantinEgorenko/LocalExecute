"""Safe, single-candidate end-to-end Swarm composition root.

This module deliberately composes existing domain components instead of adding a
second agent implementation.  A mission is intentionally bounded to one Lead
work item: it runs on an isolated Git worktree, with model shell commands
disabled, and retains the resulting candidate branch for human review.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common_runtime import create_common_runtime_components, worktree_policy as _shared_worktree_policy
from .single_endpoint import single_endpoint_registry

from ..agent_loop import CodingAgentLoop, ControlPlaneCodingAgent
from ..config import ConfigLoader
from ..context_manager import ContextBudget, ControlPlaneContextResolver, FileArtifactStore
from ..control_plane import (
    AttentionItem,
    AttentionSeverity,
    AttentionStatus,
    FileRunStore,
    Run,
    RunStatus,
)
from ..critic import CriticCoordinator, CriticPolicy
from ..executor import (
    LifecycleVerificationPolicy,
    OllamaNativeClient,
    OllamaTimeouts,
    WorkerExecutor,
    WorkerLifecycleManager,
)
from ..judge import JudgeCoordinator, JudgePolicy
from ..lead import OrnithLead, OrnithRuntime
from ..lead.models import LeadOutcome, LeadRequest
from ..models import ModelRegistry
from ..model_runtime import install_runtime_orchestration
from ..resource_manager import ResourceManager
from ..router import ResourceAwareRouter
from ..runtime import discover_servers
from ..verification import (
    BuiltinGate,
    VerificationGateKind,
    VerificationGateSpec,
    VerificationGateType,
    VerificationPlan,
    VerificationPolicy,
    VerificationPublisher,
    VerificationRunner,
)
from ..worktree import ControlPlaneIsolatedWorkspaceResolver, GitWorktreeManager, WorktreePolicy


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,96}$")
_REQUIRED_GATES = ("candidate_diff", "scope_guard", "python_syntax")


def utc_mission_id() -> str:
    """Return a filesystem-safe, collision-resistant default mission identifier."""
    return "swarm-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{label} must match {_SAFE_ID.pattern}")
    return value


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _worktree_policy(policies: dict[str, Any]) -> WorktreePolicy:
    return _shared_worktree_policy(policies)


def _git_text(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def preflight(
    *,
    swarm_root: Path,
    workspace: Path,
    state_root: Path | None = None,
    single_endpoint: bool = False,
) -> dict[str, Any]:
    """Perform a read-only launch check; no model load or worktree is created."""
    swarm_root = swarm_root.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    loader = ConfigLoader(swarm_root)
    registry = single_endpoint_registry(loader) if single_endpoint else ModelRegistry.from_loader(loader)
    issues: list[str] = []

    if not workspace.is_dir():
        issues.append("workspace is not a directory")
    else:
        try:
            target_root = Path(_git_text(workspace, "rev-parse", "--show-toplevel")).resolve(strict=True)
            target_common = _git_text(target_root, "rev-parse", "--git-common-dir")
            swarm_common = _git_text(swarm_root, "rev-parse", "--git-common-dir")
            # Git may return relative common-dir paths; normalize from each root.
            target_common_path = (target_root / target_common).resolve(strict=False)
            swarm_common_path = (swarm_root / swarm_common).resolve(strict=False)
            if target_common_path == swarm_common_path:
                issues.append("workspace is the protected Swarm control repository")
            if _git_text(target_root, "status", "--porcelain=v1", "--untracked-files=normal"):
                issues.append("workspace has uncommitted or untracked files")
        except ValueError as exc:
            issues.append(str(exc))

    if state_root is not None and _is_within(state_root, workspace):
        issues.append("state_root must be outside the target workspace")

    observations = discover_servers(registry)
    offline = [server_id for server_id, item in observations.items() if not item.online]
    if offline:
        issues.append("Ollama server offline: " + ", ".join(sorted(offline)))

    # Validate all source configuration up front, but do not load any model.
    loader.load_models()
    loader.load_resources()
    loader.load_routing()
    loader.load_policies()
    loader.load_lead()
    return {
        "status": "READY" if not issues else "BLOCKED",
        "swarm_root": str(swarm_root),
        "workspace": str(workspace),
        "state_root": None if state_root is None else str(state_root.resolve(strict=False)),
        "servers": {
            server_id: {
                "online": item.online,
                "discovery_ok": item.discovery_ok,
                "error": item.error,
                "base_url": registry.get_server(server_id).ollama_base_url,
            }
            for server_id, item in observations.items()
        },
        "physical_endpoints": sorted({server.ollama_base_url for server in registry.servers}),
        "issues": issues,
        "notes": [
            "Preflight is read-only: it does not resolve TODO tags, load models, or create a candidate worktree.",
            "Execution is available only through the Phase20V launcher, which owns GPU/CPU lifecycle guards.",
        ],
    }


@dataclass(slots=True)
class EndToEndSwarmRuntime:
    swarm_root: Path
    workspace: Path
    state_root: Path
    run_store: FileRunStore
    artifacts: FileArtifactStore
    worktrees: GitWorktreeManager
    lead: OrnithLead
    agent: ControlPlaneCodingAgent
    verification: VerificationRunner
    verification_publisher: VerificationPublisher
    critic: CriticCoordinator
    judge: JudgeCoordinator

    def run(self, *, mission_id: str, goal_id: str, objective: str) -> dict[str, Any]:
        """Run one complete Lead → candidate → verification → review mission."""
        validate_identifier(mission_id, "mission_id")
        validate_identifier(goal_id, "goal_id")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")

        request = LeadRequest(
            objective=(
                "Create exactly one atomic implementation work item for this objective. "
                "The candidate must change files; do not split the work into multiple todos.\n\n"
                + objective.strip()
            ),
            constraints={"NO_COMMANDS": True, "SWARM_E2E_SINGLE_CANDIDATE": True},
            forbidden_scope=(".git", "state"),
            metadata={"mission_id": mission_id, "mode": "single_candidate_e2e"},
        )
        planned = self.lead.decompose(request, goal_id)
        if planned.outcome is not LeadOutcome.PLAN_READY:
            return self._report(
                mission_id,
                "PLANNING_BLOCKED",
                lead=planned.to_dict(),
                reason=planned.attention_reason or "Lead did not produce a runnable plan",
            )
        if len(planned.work_items) != 1:
            return self._report(
                mission_id,
                "PLANNING_BLOCKED",
                lead=planned.to_dict(),
                reason="E2E V1 accepts exactly one atomic Lead todo; refine the objective and start a new mission.",
            )

        item = planned.work_items[0]
        run_id = f"{mission_id}-run"
        self.run_store.create_run(Run(run_id, goal_id, item.todo.todo_id, RunStatus.RUNNING, quota_usage=self._empty_usage()))
        lease = self.agent.workspace_resolver.acquire_for_run(run_id=run_id, contract=item.work_contract)
        agent_result = self.agent.execute(
            run_id=run_id,
            contract=item.work_contract,
            routing_spec=item.routing_task_spec,
            budget=ContextBudget(max_tokens=12000, max_items=48, max_bytes=192 * 1024),
            request_id=f"{run_id}:agent",
            task_id=item.todo.todo_id,
            keep_alive=-1,
        )
        snapshot = self.worktrees.snapshot(lease.lease_id)
        if not snapshot.dirty:
            self.run_store.record_attention(
                run_id,
                AttentionItem(
                    attention_id=f"{run_id}-no-candidate-diff",
                    reason="Coding agent ended without a candidate diff; deterministic verification was not run.",
                    severity=AttentionSeverity.HIGH,
                    status=AttentionStatus.OPEN,
                    todo_id=item.todo.todo_id,
                    run_id=run_id,
                    category="candidate",
                ),
            )
            return self._report(
                mission_id,
                "NEEDS_ATTENTION",
                run_id=run_id,
                lease=lease.to_dict(),
                lead=planned.to_dict(),
                agent=agent_result.to_dict(),
                reason="No candidate change was produced; candidate branch is retained for inspection.",
            )

        plan = VerificationPlan(
            plan_id=f"{run_id}-verification",
            run_id=run_id,
            todo_id=item.todo.todo_id,
            lease_id=lease.lease_id,
            gates=(
                VerificationGateSpec("candidate_diff", VerificationGateKind.BUILTIN, VerificationGateType.DIFF, True, builtin=BuiltinGate.CANDIDATE_DIFF),
                VerificationGateSpec("scope_guard", VerificationGateKind.BUILTIN, VerificationGateType.SCOPE, True, builtin=BuiltinGate.SCOPE_GUARD),
                VerificationGateSpec("python_syntax", VerificationGateKind.BUILTIN, VerificationGateType.SYNTAX, True, builtin=BuiltinGate.PYTHON_SYNTAX),
            ),
        )
        verification = self.verification.run(plan=plan, contract=item.work_contract)
        self.verification_publisher.publish(verification)
        if verification.outcome.value == "FAILED":
            current = self.run_store.get_run(run_id)
            self.run_store.update_run(Run(current.run_id, current.goal_id, current.active_todo_id, RunStatus.FAILED, current.quota_usage))

        critic_result = self.critic.review(run_id=run_id, contract=item.work_contract, lease_id=lease.lease_id)
        judge_result = self.judge.review(run_id=run_id, contract=item.work_contract, lease_id=lease.lease_id)
        accepted = verification.outcome.value == "PASSED" and judge_result.report.analysis.decision.value == "ACCEPT"
        if not accepted and self.run_store.get_run(run_id).status is not RunStatus.FAILED:
            current = self.run_store.get_run(run_id)
            self.run_store.update_run(Run(current.run_id, current.goal_id, current.active_todo_id, RunStatus.NEEDS_ATTENTION, current.quota_usage))
        return self._report(
            mission_id,
            "ACCEPTED" if accepted else "NEEDS_ATTENTION",
            run_id=run_id,
            lease=lease.to_dict(),
            lead=planned.to_dict(),
            agent=agent_result.to_dict(),
            verification=verification.to_dict(),
            critic=critic_result.report.to_dict(),
            judge=judge_result.report.to_dict(),
        )

    @staticmethod
    def _empty_usage():
        from ..control_plane import QuotaUsage

        return QuotaUsage()

    def _report(self, mission_id: str, status: str, **payload: Any) -> dict[str, Any]:
        report = {
            "mission_id": mission_id,
            "status": status,
            "workspace": str(self.workspace),
            "state_root": str(self.state_root),
            "candidate_retention": "ACTIVE worktree and branch are retained; no source branch is modified.",
            **payload,
        }
        reports = self.state_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        destination = reports / f"{mission_id}.json"
        report["report_path"] = str(destination)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=reports, delete=False) as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, destination)
        return report


def create_end_to_end_runtime(*, swarm_root: Path, workspace: Path, state_root: Path) -> EndToEndSwarmRuntime:
    """Construct the explicit smoke-single-candidate profile."""
    common = create_common_runtime_components(
        swarm_root=swarm_root,
        workspace=workspace,
        state_root=state_root,
    )
    return EndToEndSwarmRuntime(
        swarm_root=common.swarm_root,
        workspace=common.workspace,
        state_root=common.state_root,
        run_store=common.run_store,
        artifacts=common.artifacts,
        worktrees=common.worktrees,
        lead=common.lead,
        agent=common.agent,
        verification=common.verification_runner,
        verification_publisher=common.verification_publisher,
        critic=common.critic,
        judge=common.judge,
    )
