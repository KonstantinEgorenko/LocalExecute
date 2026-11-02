'research campaign.R6 bridge from Research ExperimentSpec to the existing Swarm runtime.\n\nThe bridge deliberately does not create a second executor or scheduler.  It\nmaterializes one durable mission MissionSpec, seeds the exact experiment code\nbase into the existing workflow project-snapshot spine, invokes the existing\nproduction runtime, and projects authoritative verification/worktree lineage\nback into ExperimentExecutionOutcomeV1.\n'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from ..context_manager.integration import artifact_uri
from ..workflow.models import ProjectSnapshotV1
from ..mission.models import MissionSpecV1, MissionState
from ..verification.models import VerificationOutcome
from ..verification.publisher import VerificationPublisher
from ..worktree.git import GitClient
from .models import ExperimentFailureKindV1, ExperimentSpecV1
from .ports import (
    ExperimentExecutionOutcomeV1,
    ExperimentExecutionStatusV1,
)
from .serialization import freeze_json, plain, stable_sha256, text


_EMPTY_DIFF_SHA256 = hashlib.sha256(b"").hexdigest()


class SwarmExperimentExecutionError(RuntimeError):
    """Base class for invariant/configuration failures at the R6 seam."""


class SwarmExperimentExecutionConfigurationError(SwarmExperimentExecutionError):
    """ExperimentSpec.execution cannot be projected into a durable Swarm mission."""


class SwarmExperimentExecutionIntegrityError(SwarmExperimentExecutionError):
    """Persisted Mission/Run/Verification lineage is missing or contradictory."""


class ResearchExecutionModeV1(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    CUMULATIVE = "CUMULATIVE"


_RESERVED_CONSTRAINT_KEYS = frozenset(
    {
        "workspace",
        "research_campaign_id",
        "research_experiment_id",
        "research_experiment_fingerprint",
        "research_experiment_parameters",
        "research_hypothesis_id",
        "research_strategy_id",
        "research_execution_mode",
        "research_code_base_commit",
        "research_dataset_fingerprint",
        "research_evaluation_contract_hash",
        "research_seed",
        "research_model_id",
        "research_provider",
        "research_model_version",
        "research_prompt_hash",
        "research_tool_policy_hash",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class SwarmExperimentExecutionConfigV1:
    """Strict projection of ``ExperimentSpecV1.execution`` into Phase17.

    ``code_base_commit`` remains on ExperimentSpecV1 because it participates in
    the logical experiment fingerprint.  R6 only chooses how that exact commit
    is seeded into Phase16.
    """

    objective: str
    goal_capsule: str
    mode: ResearchExecutionModeV1 = ResearchExecutionModeV1.INDEPENDENT
    global_constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", text(self.objective, "objective", max_length=2_000_000))
        object.__setattr__(self, "goal_capsule", text(self.goal_capsule, "goal_capsule", max_length=100_000))
        if not isinstance(self.mode, ResearchExecutionModeV1):
            raise ValueError("mode must be ResearchExecutionModeV1")
        frozen = freeze_json(self.global_constraints, "global_constraints")
        if not isinstance(frozen, Mapping):
            raise ValueError("global_constraints must be mapping")
        collisions = _RESERVED_CONSTRAINT_KEYS & set(frozen)
        if collisions:
            raise ValueError(
                "global_constraints cannot override R6 reserved keys: " + ", ".join(sorted(collisions))
            )
        object.__setattr__(self, "global_constraints", frozen)

    @classmethod
    def from_spec(cls, spec: ExperimentSpecV1) -> "SwarmExperimentExecutionConfigV1":
        raw = spec.execution
        objective = raw.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise SwarmExperimentExecutionConfigurationError(
                "ExperimentSpec.execution.objective is required for Swarm execution"
            )
        capsule_raw = raw.get("goal_capsule", objective)
        if not isinstance(capsule_raw, str) or not capsule_raw.strip():
            raise SwarmExperimentExecutionConfigurationError(
                "ExperimentSpec.execution.goal_capsule must be non-empty string when present"
            )
        mode_raw = raw.get("mode", ResearchExecutionModeV1.INDEPENDENT.value)
        if not isinstance(mode_raw, str):
            raise SwarmExperimentExecutionConfigurationError("ExperimentSpec.execution.mode must be string")
        try:
            mode = ResearchExecutionModeV1(mode_raw)
        except ValueError as exc:
            raise SwarmExperimentExecutionConfigurationError(f"unsupported research execution mode: {mode_raw}") from exc
        constraints = raw.get("global_constraints", {})
        if not isinstance(constraints, Mapping):
            raise SwarmExperimentExecutionConfigurationError(
                "ExperimentSpec.execution.global_constraints must be mapping"
            )
        try:
            return cls(objective=objective, goal_capsule=capsule_raw, mode=mode, global_constraints=constraints)
        except ValueError as exc:
            raise SwarmExperimentExecutionConfigurationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SwarmTodoExecutionLineageV1:
    todo_id: str
    run_id: str
    verification_report_id: str
    verification_artifact_ref: str
    verified_outcome_ref: str
    candidate_head: str
    candidate_diff_sha256: str

    def __post_init__(self) -> None:
        for name in ("todo_id", "run_id", "verification_report_id", "verification_artifact_ref", "verified_outcome_ref"):
            text(getattr(self, name), name, max_length=32_000)
        candidate_head = text(self.candidate_head, "candidate_head", max_length=40)
        candidate_diff = text(self.candidate_diff_sha256, "candidate_diff_sha256", max_length=64)
        if len(candidate_head) != 40:
            raise ValueError("candidate_head must be full 40-character Git commit")
        if len(candidate_diff) != 64:
            raise ValueError("candidate_diff_sha256 must be 64-character SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "run_id": self.run_id,
            "verification_report_id": self.verification_report_id,
            "verification_artifact_ref": self.verification_artifact_ref,
            "verified_outcome_ref": self.verified_outcome_ref,
            "candidate_head": self.candidate_head,
            "candidate_diff_sha256": self.candidate_diff_sha256,
        }


@dataclass(frozen=True, slots=True)
class SwarmExecutionLineageV1:
    mission_id: str
    goal_id: str
    base_commit: str
    candidate_commit: str | None
    completed_todo_ids: tuple[str, ...]
    todos: tuple[SwarmTodoExecutionLineageV1, ...]
    project_snapshot_ref: str | None
    project_snapshot_epoch: int | None
    final_report_ref: str | None
    candidate_worktree_root: str | None

    def __post_init__(self) -> None:
        for name in ("mission_id", "goal_id", "base_commit"):
            text(getattr(self, name), name, max_length=32_000)
        if len(self.base_commit) != 40:
            raise ValueError("base_commit must be full 40-character Git commit")
        if self.candidate_commit is not None and len(self.candidate_commit) != 40:
            raise ValueError("candidate_commit must be full 40-character Git commit")
        if len(set(self.completed_todo_ids)) != len(self.completed_todo_ids):
            raise ValueError("completed_todo_ids must be unique")
        if any(not isinstance(item, SwarmTodoExecutionLineageV1) for item in self.todos):
            raise ValueError("todos must contain SwarmTodoExecutionLineageV1")
        if tuple(item.todo_id for item in self.todos) != self.completed_todo_ids:
            raise ValueError("todo lineage order must match completed_todo_ids")
        if self.project_snapshot_epoch is not None and self.project_snapshot_epoch < 0:
            raise ValueError("project_snapshot_epoch must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "completed_todo_ids": list(self.completed_todo_ids),
            "todos": [item.to_dict() for item in self.todos],
            "project_snapshot_ref": self.project_snapshot_ref,
            "project_snapshot_epoch": self.project_snapshot_epoch,
            "final_report_ref": self.final_report_ref,
            "candidate_worktree_root": self.candidate_worktree_root,
        }


class ProductionSwarmExperimentExecutionPortV1:
    """R6 ``ExperimentExecutionPort`` implemented by the existing Swarm core."""

    @legacy_keywords({'require_phase20v_environment': 'require_model_runtime_environment'})
    def __init__(
        self,
        *,
        swarm_root: str | Path,
        workspace: str | Path,
        state_root: str | Path,
        runtime_factory: Callable[..., Any] | None = None,
        require_model_runtime_environment: bool = True,
        clock: Callable[[], str] = _utc_now,
        git: GitClient | None = None,
    ) -> None:
        self.swarm_root = Path(swarm_root).resolve(strict=True)
        self.workspace = Path(workspace).resolve(strict=True)
        self.state_root = Path(state_root).resolve(strict=False)
        if self._within(self.state_root, self.workspace):
            raise ValueError("research execution state_root must be outside workspace")
        self.require_model_runtime_environment = require_model_runtime_environment
        self.clock = clock
        self.git = git or GitClient()
        if runtime_factory is None:
            from ..integration.runtime_factory import create_production_swarm_runtime

            runtime_factory = create_production_swarm_runtime
        self.runtime_factory = runtime_factory

    @staticmethod
    def _within(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def identities(spec: ExperimentSpecV1) -> tuple[str, str]:
        digest = stable_sha256(
            {
                "campaign_id": spec.campaign_id,
                "experiment_id": spec.experiment_id,
                "fingerprint": spec.fingerprint,
            }
        )[:28]
        return f"r22m-{digest}", f"r22g-{digest}"

    def experiment_state_root(self, spec: ExperimentSpecV1) -> Path:
        return self.state_root / spec.campaign_id / spec.experiment_id

    def _resolve_base_commit(self, spec: ExperimentSpecV1) -> str:
        if len(spec.code_base_commit) != 40:
            raise SwarmExperimentExecutionConfigurationError(
                "R6 requires ExperimentSpec.code_base_commit to be a full 40-character commit"
            )
        resolved = self.git.text(
            self.workspace,
            "rev-parse",
            "--verify",
            f"{spec.code_base_commit}^{{commit}}",
        ).lower()
        if resolved != spec.code_base_commit:
            raise SwarmExperimentExecutionIntegrityError(
                "ExperimentSpec.code_base_commit does not resolve to the exact requested commit"
            )
        dirty = self.git.text(self.workspace, "status", "--porcelain=v1", "--untracked-files=normal")
        if dirty:
            raise SwarmExperimentExecutionConfigurationError(
                "source workspace must be clean before starting/resuming a research experiment"
            )
        return resolved

    def _mission_spec(
        self,
        spec: ExperimentSpecV1,
        config: SwarmExperimentExecutionConfigV1,
        *,
        base_commit: str,
    ) -> MissionSpecV1:
        mission_id, goal_id = self.identities(spec)
        constraints = dict(plain(config.global_constraints))
        provenance = {
            "workspace": str(self.workspace),
            "research_campaign_id": spec.campaign_id,
            "research_experiment_id": spec.experiment_id,
            "research_experiment_fingerprint": spec.fingerprint,
            "research_experiment_parameters": plain(spec.parameters),
            "research_hypothesis_id": spec.hypothesis_id,
            "research_strategy_id": spec.strategy_id,
            "research_execution_mode": config.mode.value,
            "research_code_base_commit": base_commit,
            "research_dataset_fingerprint": spec.dataset_fingerprint,
            "research_evaluation_contract_hash": spec.evaluation_contract_hash,
            "research_seed": spec.seed,
            "research_model_id": spec.model_id,
            "research_provider": spec.provider,
            "research_model_version": spec.model_version,
            "research_prompt_hash": spec.prompt_hash,
            "research_tool_policy_hash": spec.tool_policy_hash,
        }
        constraints.update(provenance)
        return MissionSpecV1(
            mission_id=mission_id,
            goal_id=goal_id,
            raw_objective=config.objective,
            goal_capsule=config.goal_capsule,
            global_constraints=constraints,
        )

    def _runtime(self, spec: ExperimentSpecV1) -> Any:
        return self.runtime_factory(
            swarm_root=self.swarm_root,
            workspace=self.workspace,
            state_root=self.experiment_state_root(spec),
            require_model_runtime_environment=self.require_model_runtime_environment,
        )

    @staticmethod
    def _require_runtime_shape(runtime: Any) -> None:
        if getattr(runtime, "mission_store", None) is None:
            raise SwarmExperimentExecutionIntegrityError("Swarm runtime is missing mission_store")
        if getattr(runtime, "external_context_spine", None) is None:
            raise SwarmExperimentExecutionIntegrityError("Swarm runtime is missing external_context_spine")
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None or getattr(scheduler, "workspace", None) is None:
            raise SwarmExperimentExecutionIntegrityError("Swarm runtime is missing production scheduler/workspace seam")

    @staticmethod
    def _run_store(runtime: Any) -> Any:
        common = getattr(runtime, "common", None)
        store = getattr(common, "run_store", None) if common is not None else None
        if store is None:
            store = getattr(runtime.scheduler.workspace, "run_store", None)
        if store is None:
            raise SwarmExperimentExecutionIntegrityError("Swarm runtime is missing authoritative RunStore")
        return store

    @staticmethod
    def _artifact_store(runtime: Any) -> Any:
        spine = runtime.external_context_spine
        store = getattr(spine, "artifact_store", None)
        if store is None:
            raise SwarmExperimentExecutionIntegrityError("Swarm context spine is missing artifact_store")
        return store

    @staticmethod
    def _completed_run_id(runtime: Any, todo_id: str) -> str:
        workspace = runtime.scheduler.workspace
        method = getattr(workspace, "completed_run_id", None)
        if callable(method):
            run_id = method(todo_id)
            if isinstance(run_id, str) and run_id:
                return run_id
        runs = getattr(ProductionSwarmExperimentExecutionPortV1._run_store(runtime), "_runs", {})
        candidates = sorted(
            run.run_id
            for run in runs.values()
            if getattr(run, "active_todo_id", None) == todo_id and getattr(getattr(run, "status", None), "value", None) == "COMPLETED"
        )
        if not candidates:
            raise SwarmExperimentExecutionIntegrityError(f"missing completed execution Run for Todo {todo_id}")
        return candidates[-1]

    def _seed_base_snapshot(self, runtime: Any, mission_spec: MissionSpecV1, base_commit: str) -> str:
        self._require_runtime_shape(runtime)
        snapshot = ProjectSnapshotV1(
            mission_spec.goal_id,
            0,
            base_commit,
            _EMPTY_DIFF_SHA256,
            (),
        )
        ref = runtime.external_context_spine.publish_project_snapshot(snapshot)
        return artifact_uri(ref.artifact_id)

    def _candidate_worktree_root(self, runtime: Any, completed_todo_ids: tuple[str, ...]) -> str | None:
        if not completed_todo_ids:
            return None
        workspace = runtime.scheduler.workspace
        current = getattr(workspace, "current", None)
        if not callable(current):
            return None
        try:
            _run_id, lease = current(completed_todo_ids[-1])
        except Exception:
            return None
        root = getattr(lease, "worktree_root", None)
        return str(root) if isinstance(root, str) and root else None

    def _collect_lineage(
        self,
        runtime: Any,
        spec: ExperimentSpecV1,
        mission_spec: MissionSpecV1,
        mission_result: Any,
        *,
        require_complete: bool,
    ) -> SwarmExecutionLineageV1:
        self._require_runtime_shape(runtime)
        checkpoint = runtime.mission_store.load(mission_spec.mission_id)
        if checkpoint.spec.to_dict() != mission_spec.to_dict():
            raise SwarmExperimentExecutionIntegrityError("durable MissionSpec does not match experiment projection")

        completed = tuple(mission_result.completed_todo_ids)
        if require_complete and tuple(checkpoint.completed_todo_ids) != completed:
            raise SwarmExperimentExecutionIntegrityError("MissionRunResult/checkpoint completed Todo lineage mismatch")

        run_store = self._run_store(runtime)
        artifacts = self._artifact_store(runtime)
        verification_publisher = VerificationPublisher(run_store=run_store, artifact_store=artifacts)
        todo_lineage: list[SwarmTodoExecutionLineageV1] = []

        for todo_id in completed:
            try:
                verified, verified_ref = runtime.external_context_spine.verified_outcome(mission_spec.goal_id, todo_id)
            except Exception as exc:
                if require_complete:
                    raise SwarmExperimentExecutionIntegrityError(
                        f"missing verified outcome for completed Todo {todo_id}: {exc}"
                    ) from exc
                continue
            run_id = self._completed_run_id(runtime, todo_id)
            run = run_store.get_run(run_id)
            if getattr(getattr(run, "status", None), "value", None) != "COMPLETED":
                raise SwarmExperimentExecutionIntegrityError(f"accepted Run is not COMPLETED for Todo {todo_id}")
            if getattr(run, "active_todo_id", None) != todo_id:
                raise SwarmExperimentExecutionIntegrityError(f"accepted Run/Todo identity mismatch for {todo_id}")

            report = verification_publisher.find_existing(report_id=verified.verification_report_id)
            if report is None:
                if require_complete:
                    raise SwarmExperimentExecutionIntegrityError(
                        f"missing persisted VerificationReport {verified.verification_report_id}"
                    )
                continue
            if report.run_id != run_id or report.todo_id != todo_id:
                raise SwarmExperimentExecutionIntegrityError("VerificationReport Run/Todo lineage mismatch")
            if report.outcome is not VerificationOutcome.PASSED:
                raise SwarmExperimentExecutionIntegrityError("completed Todo references non-passing VerificationReport")
            if verified.verification_fingerprint != report.plan_fingerprint:
                raise SwarmExperimentExecutionIntegrityError("verified outcome/report fingerprint mismatch")

            todo_lineage.append(
                SwarmTodoExecutionLineageV1(
                    todo_id=todo_id,
                    run_id=run_id,
                    verification_report_id=report.report_id,
                    verification_artifact_ref=artifact_uri(verification_publisher.artifact_id(report.report_id)),
                    verified_outcome_ref=artifact_uri(verified_ref.artifact_id),
                    candidate_head=report.candidate_head,
                    candidate_diff_sha256=report.candidate_diff_sha256,
                )
            )

        snapshot_ref_value: str | None = None
        snapshot_epoch: int | None = None
        candidate_commit: str | None = None
        try:
            snapshot, snapshot_ref = runtime.external_context_spine.latest_project_snapshot(mission_spec.goal_id)
            snapshot_ref_value = artifact_uri(snapshot_ref.artifact_id)
            snapshot_epoch = snapshot.epoch
            candidate_commit = snapshot.head_commit.lower()
        except Exception as exc:
            if require_complete:
                raise SwarmExperimentExecutionIntegrityError(f"missing final project snapshot: {exc}") from exc

        if require_complete:
            if mission_result.state is not MissionState.COMPLETED:
                raise SwarmExperimentExecutionIntegrityError("complete lineage requested for non-COMPLETED mission")
            if len(todo_lineage) != len(completed):
                raise SwarmExperimentExecutionIntegrityError("completed mission lacks complete Todo verification lineage")
            if snapshot_epoch != len(completed):
                raise SwarmExperimentExecutionIntegrityError(
                    "final project snapshot epoch does not equal completed Todo count"
                )
            if candidate_commit is None or len(candidate_commit) != 40:
                raise SwarmExperimentExecutionIntegrityError("final candidate commit is not a full Git commit")
            resolved = self.git.text(
                self.workspace,
                "rev-parse",
                "--verify",
                f"{candidate_commit}^{{commit}}",
            ).lower()
            if resolved != candidate_commit:
                raise SwarmExperimentExecutionIntegrityError("final candidate commit is not resolvable from source repository")
            ancestry = self.git.run(
                self.workspace,
                "merge-base",
                "--is-ancestor",
                spec.code_base_commit,
                candidate_commit,
                check=False,
            )
            if ancestry.returncode != 0:
                raise SwarmExperimentExecutionIntegrityError(
                    "final candidate commit is not descended from ExperimentSpec.code_base_commit"
                )

        return SwarmExecutionLineageV1(
            mission_id=mission_spec.mission_id,
            goal_id=mission_spec.goal_id,
            base_commit=spec.code_base_commit,
            candidate_commit=candidate_commit if require_complete else None,
            completed_todo_ids=tuple(item.todo_id for item in todo_lineage),
            todos=tuple(todo_lineage),
            project_snapshot_ref=snapshot_ref_value,
            project_snapshot_epoch=snapshot_epoch,
            final_report_ref=mission_result.final_report_ref,
            candidate_worktree_root=self._candidate_worktree_root(runtime, completed),
        )

    @staticmethod
    def _aggregate_run_telemetry(
        runtime: Any,
        lineage: SwarmExecutionLineageV1,
        *,
        goal_id: str,
    ) -> dict[str, Any]:
        """Aggregate every physical Run attempted for the mission goal.

        Completed-Todo lineage is intentionally strict and verification-backed,
        but failed/blocked missions can have substantial worker activity without
        any completed Todo. Research telemetry must retain those physical attempts
        rather than reporting a misleading zero-call execution.
        """
        run_store = ProductionSwarmExperimentExecutionPortV1._run_store(runtime)
        raw_runs = getattr(run_store, "_runs", None)
        if isinstance(raw_runs, dict):
            runs = sorted(
                (run for run in raw_runs.values() if getattr(run, "goal_id", None) == goal_id),
                key=lambda run: run.run_id,
            )
        else:
            runs = [run_store.get_run(item.run_id) for item in lineage.todos]

        worker_calls = replans = escalations = 0
        worker_attempts: dict[str, int] = {}
        selected_models: list[str] = []
        attempted_runs: list[dict[str, Any]] = []
        for run in runs:
            usage = run.quota_usage
            worker_calls += usage.worker_calls
            replans += usage.replans
            escalations += usage.escalations
            for model_id, count in usage.worker_attempts.items():
                worker_attempts[model_id] = worker_attempts.get(model_id, 0) + count
            attention: list[dict[str, Any]] = []
            for event in run_store.list_events(run.run_id):
                event_type = getattr(getattr(event, "event_type", None), "value", None)
                payload = event.to_dict().get("payload", {})
                if event_type == "ATTEMPT_RECORDED":
                    selected = payload.get("selected_model")
                    if isinstance(selected, str) and selected and selected not in selected_models:
                        selected_models.append(selected)
                elif event_type == "ATTENTION_RECORDED" and isinstance(payload, dict):
                    attention.append({
                        "attention_id": payload.get("attention_id"),
                        "category": payload.get("category"),
                        "reason": payload.get("reason"),
                        "severity": payload.get("severity"),
                        "status": payload.get("status"),
                    })
            attempted_runs.append({
                "run_id": run.run_id,
                "todo_id": run.active_todo_id,
                "status": run.status.value,
                "worker_calls": usage.worker_calls,
                "replans": usage.replans,
                "escalations": usage.escalations,
                "worker_attempts": dict(usage.worker_attempts),
                "attention": attention,
            })
        return {
            "worker_calls": worker_calls,
            "replans": replans,
            "escalations": escalations,
            "worker_attempts": worker_attempts,
            "selected_models": selected_models,
            "attempted_run_ids": [item["run_id"] for item in attempted_runs],
            "attempted_runs": attempted_runs,
        }

    @staticmethod
    def _failure_kind_from_reason(reason: str | None) -> ExperimentFailureKindV1:
        value = (reason or "").casefold()
        if "out of memory" in value or "oom" in value or "vram" in value:
            return ExperimentFailureKindV1.OOM
        if "timeout" in value or "timed out" in value:
            return ExperimentFailureKindV1.TIMEOUT
        if any(token in value for token in ("permission", "approval", "credential", "token required")):
            return ExperimentFailureKindV1.PERMISSION_REQUIRED
        if any(
            token in value
            for token in (
                "resource unavailable",
                "model unavailable",
                "capacity unavailable",
                "required phase16 context cannot fit",
                "candidate worktree path already exists without a durable active lease",
                "candidate branch already exists without a durable active lease",
            )
        ):
            return ExperimentFailureKindV1.RESOURCE_UNAVAILABLE
        if "dataset" in value or "data error" in value:
            return ExperimentFailureKindV1.DATA_ERROR
        if "training" in value and ("crash" in value or "failed" in value):
            return ExperimentFailureKindV1.TRAINING_CRASH
        return ExperimentFailureKindV1.IMPLEMENTATION_BUG

    @staticmethod
    def _failure_kind_from_run_telemetry(telemetry: Mapping[str, Any]) -> ExperimentFailureKindV1 | None:
        'Recover technical failure taxonomy hidden behind a generic mission reason.\n\n        recovery may intentionally terminalize a bounded execution as HUMAN_REVIEW even\n        when the deterministic producer already classified the physical Run as a quota\n        or tool/agent-loop limit.  Research memory must not reinterpret that technical\n        stop as a candidate-owned implementation defect.  The attempted-run attention\n        ledger is durable, typed operational evidence and is therefore stronger than\n        the generic outer mission reason for this narrow classification purpose.\n        '
        attempted = telemetry.get("attempted_runs")
        if not isinstance(attempted, (tuple, list)):
            return None
        for run in attempted:
            if not isinstance(run, Mapping):
                continue
            attention = run.get("attention")
            if not isinstance(attention, (tuple, list)):
                continue
            for item in attention:
                if not isinstance(item, Mapping):
                    continue
                category = str(item.get("category") or "").casefold()
                reason = str(item.get("reason") or "").casefold()
                if category == "quota" or "quota exhausted" in reason:
                    return ExperimentFailureKindV1.RESOURCE_UNAVAILABLE
                if reason in {
                    "tool failure limit reached",
                    "repeated identical tool call limit reached",
                }:
                    return ExperimentFailureKindV1.RESOURCE_UNAVAILABLE
        return None

    @classmethod
    def _failure_kind_from_mission_failure(
        cls,
        reason: str | None,
        telemetry: Mapping[str, Any],
    ) -> ExperimentFailureKindV1:
        # Preserve explicit outer failure semantics first.  Only generic
        # IMPLEMENTATION_BUG fallback is eligible for refinement from physical Run
        # telemetry, so TIMEOUT/OOM/PERMISSION/etc. keep their existing authority.
        outer = cls._failure_kind_from_reason(reason)
        if outer is not ExperimentFailureKindV1.IMPLEMENTATION_BUG:
            return outer
        technical = cls._failure_kind_from_run_telemetry(telemetry)
        return outer if technical is None else technical

    @staticmethod
    def _failure_kind_from_exception(exc: Exception) -> ExperimentFailureKindV1:
        if isinstance(exc, TimeoutError):
            return ExperimentFailureKindV1.TIMEOUT
        if isinstance(exc, MemoryError):
            return ExperimentFailureKindV1.OOM
        if isinstance(exc, PermissionError):
            return ExperimentFailureKindV1.PERMISSION_REQUIRED
        return ProductionSwarmExperimentExecutionPortV1._failure_kind_from_reason(str(exc))

    def execute(self, spec: ExperimentSpecV1, *, resume: bool) -> ExperimentExecutionOutcomeV1:
        if not isinstance(spec, ExperimentSpecV1):
            raise TypeError("spec must be ExperimentSpecV1")
        if not isinstance(resume, bool):
            raise TypeError("resume must be bool")

        started_at = self.clock()
        config = SwarmExperimentExecutionConfigV1.from_spec(spec)
        base_commit = self._resolve_base_commit(spec)
        mission_spec = self._mission_spec(spec, config, base_commit=base_commit)

        try:
            runtime = self._runtime(spec)
            self._require_runtime_shape(runtime)
            seed_ref = self._seed_base_snapshot(runtime, mission_spec, base_commit)
            mission_result = runtime.run_or_resume(mission_spec)
            completed = mission_result.state is MissionState.COMPLETED
            lineage = self._collect_lineage(
                runtime,
                spec,
                mission_spec,
                mission_result,
                require_complete=completed,
            )
            ended_at = self.clock()

            telemetry: dict[str, Any] = {
                "execution_backend": "SWARM_PHASE17_PHASE16",
                "research_execution_mode": config.mode.value,
                "research_resume_requested": resume,
                "experiment_state_root": str(self.experiment_state_root(spec)),
                "mission_id": mission_spec.mission_id,
                "goal_id": mission_spec.goal_id,
                "checkpoint_generation": mission_result.checkpoint_generation,
                "phase16_invocations": mission_result.workflow_invocations,
                "restart_count": mission_result.restart_count,
                "amendment_count": mission_result.amendment_count,
                "base_snapshot_ref": seed_ref,
                "final_report_ref": mission_result.final_report_ref,
                "mission_state": mission_result.state.value,
                "mission_reason": mission_result.reason,
                "swarm_lineage": lineage.to_dict(),
            }
            telemetry.update(self._aggregate_run_telemetry(runtime, lineage, goal_id=mission_spec.goal_id))
            if lineage.candidate_worktree_root is not None:
                telemetry["candidate_worktree_root"] = lineage.candidate_worktree_root

            artifact_refs: list[str] = []
            if lineage.project_snapshot_ref is not None:
                artifact_refs.append(lineage.project_snapshot_ref)
            for item in lineage.todos:
                artifact_refs.extend((item.verified_outcome_ref, item.verification_artifact_ref))
            artifact_refs = list(dict.fromkeys(artifact_refs))

            if completed:
                return ExperimentExecutionOutcomeV1(
                    status=ExperimentExecutionStatusV1.COMPLETED,
                    started_at=started_at,
                    ended_at=ended_at,
                    verification_outcome="PASSED",
                    artifact_refs=tuple(artifact_refs),
                    candidate_commit=lineage.candidate_commit,
                    telemetry=telemetry,
                )

            failure_kind = self._failure_kind_from_mission_failure(mission_result.reason, telemetry)
            telemetry["research_failure_kind"] = failure_kind.value
            status = (
                ExperimentExecutionStatusV1.CRASHED
                if mission_result.state is MissionState.FAILED
                else ExperimentExecutionStatusV1.FAILED
            )
            return ExperimentExecutionOutcomeV1(
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                verification_outcome="FAILED" if lineage.todos else None,
                artifact_refs=tuple(artifact_refs),
                telemetry=telemetry,
                failure_kind=failure_kind,
            )
        except SwarmExperimentExecutionError:
            raise
        except Exception as exc:
            ended_at = self.clock()
            return ExperimentExecutionOutcomeV1(
                status=ExperimentExecutionStatusV1.CRASHED,
                started_at=started_at,
                ended_at=ended_at,
                telemetry={
                    "execution_backend": "SWARM_PHASE17_PHASE16",
                    "research_execution_mode": config.mode.value,
                    "research_resume_requested": resume,
                    "experiment_state_root": str(self.experiment_state_root(spec)),
                    "mission_id": mission_spec.mission_id,
                    "goal_id": mission_spec.goal_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                failure_kind=self._failure_kind_from_exception(exc),
            )
