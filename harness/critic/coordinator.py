'critic bounded Ministral critic orchestration.'
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..errors import ExecutionError
from ..executor.models import ExecutionOutcome, ExecutionRequest
from ..orchestrator.models import RoutingTaskSpec
from ..orchestrator.profile_factory import TaskProfileFactory
from ..router.profile import Complexity, ContextBucket, ContextEstimate, NeedLevel, Scope, TaskType, ValidationLevel
from ..router.scoring import DecisionType
from ..worktree.manager import GitWorktreeManager
from .errors import CriticExhaustedError, CriticIntegrityError, CriticProtocolError, CriticRoutingError, CriticConformanceError
from .evidence import CriticEvidenceResolver
from .models import (
    CriticCoordinatorResult, CriticPolicy, CriticReport, CriticTurnRecord, CriticTurnStatus,
)
from .prompt import CriticPromptBuilder
from .protocol import parse_critic_response
from .publisher import CriticPublisher


class CriticCoordinator:
    def __init__(
        self,
        *,
        run_store: Any,
        artifact_store: Any,
        manager: GitWorktreeManager,
        router: Any,
        executor: Any,
        snapshot_provider: Callable[[], Any],
        policy: CriticPolicy,
        profile_factory: TaskProfileFactory | None = None,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.manager = manager
        self.router = router
        self.executor = executor
        self.snapshot_provider = snapshot_provider
        self.policy = policy
        self.factory = profile_factory or TaskProfileFactory()
        self.resolver = CriticEvidenceResolver(run_store=run_store, artifact_store=artifact_store, manager=manager, policy=policy)
        self.publisher = CriticPublisher(run_store=run_store, artifact_store=artifact_store)
        self.prompts = CriticPromptBuilder(policy)

    def review(self, *, run_id: str, contract: Any, lease_id: str) -> CriticCoordinatorResult:
        bundle = self.resolver.resolve(run_id=run_id, contract=contract, lease_id=lease_id)
        from .evidence_aliases import alias_mode, prepare_manifest, verify_manifest, resolve_alias_response, record_resolution, claim_dispatch, verify_dispatch, dispatch_id
        aliases = alias_mode(bundle)
        stores = dict(manager=self.manager, run_store=self.run_store, artifact_store=self.artifact_store)
        manifest = None
        if aliases:
            try:
                consumed = self.artifact_store.has_artifact(dispatch_id(bundle))
                manifest = (verify_manifest if consumed else prepare_manifest)(bundle, lease_id, **stores)
                if consumed:
                    verify_dispatch(bundle, manifest, self.artifact_store)
                    if not self.artifact_store.has_artifact(self.publisher.turn_artifact_id(bundle.request_id,0)):
                        raise CriticConformanceError('Critic dispatch was reserved but response is absent; replay forbidden')
            except Exception as exc:
                raise CriticConformanceError(str(exc), {'request_id':bundle.request_id,'run_id':run_id,'todo_id':bundle.todo_id,'lease_id':lease_id}) from exc
        existing = self.publisher.find_report(bundle.request_id)
        if existing is not None:
            self.publisher.validate_report(existing, bundle)
            if aliases:
                manifest = verify_manifest(bundle, lease_id, **stores)
                turn = self.publisher.find_turn(bundle.request_id, 0)
                if turn is None or turn.content is None:
                    raise CriticConformanceError('missing alias raw turn')
                canonical = resolve_alias_response(turn.content, manifest)
                analysis = parse_critic_response(canonical, bundle)
                record_resolution(bundle,artifact_store=self.artifact_store,manifest=manifest,raw_content=turn.content,resolved_content=canonical,error=None)
                if analysis != existing.analysis:
                    raise CriticConformanceError('persisted alias report differs from authoritative raw turn')
            self.publisher.publish_report(existing)
            return CriticCoordinatorResult(existing, True, 0, 0)

        baseline = self.manager.snapshot(lease_id)
        model_calls = 0
        reused_turns = 0
        prior_error: str | None = None
        prior_response: str | None = None
        last_error = "critic produced no usable response"
        for attempt in range(1 if aliases else self.policy.max_model_attempts):
            turn = self.publisher.find_turn(bundle.request_id, attempt)
            if turn is not None:
                reused_turns += 1
            else:
                decision = self._route(bundle)
                messages = self.prompts.messages(bundle, prior_error=prior_error, prior_response=prior_response, alias_manifest=manifest)
                if aliases:
                    claim_dispatch(bundle, manifest, self.artifact_store)
                try:
                    worker = self.executor.execute(ExecutionRequest(
                        decision,
                        messages,
                        self.policy.keep_alive,
                        request_id=f"{bundle.request_id}:attempt:{attempt}",
                        task_id=bundle.todo_id,
                        response_format="json",
                        temperature=self.policy.temperature,
                    ))
                    model_calls += 1
                    if worker.outcome is not ExecutionOutcome.SUCCESS:
                        raise CriticProtocolError("critic executor returned non-success WorkerResult")
                    if worker.model_id != self.policy.model_id:
                        raise CriticProtocolError("critic response came from unexpected model")
                    if worker.tool_calls:
                        raise CriticProtocolError("critic must not return tool calls")
                    if worker.content is None or len(worker.content.encode("utf-8")) > self.policy.max_response_bytes:
                        raise CriticProtocolError("critic response is missing or exceeds max_response_bytes")
                    turn = CriticTurnRecord(
                        bundle.request_id, attempt, CriticTurnStatus.RESPONSE, worker.model_id, worker.model_tag,
                        worker.content, None, worker.prompt_eval_count, worker.eval_count, worker.total_duration,
                    )
                except ExecutionError as exc:
                    model_calls += 1
                    turn = CriticTurnRecord(bundle.request_id, attempt, CriticTurnStatus.EXECUTION_ERROR, self.policy.model_id, None, None, f"{type(exc).__name__}: {exc}")
                self.publisher.publish_turn(turn)
                self._assert_candidate_unchanged(lease_id, baseline)

            if turn.status is CriticTurnStatus.EXECUTION_ERROR:
                if aliases:
                    raise CriticConformanceError(turn.error or 'critic execution failure', {'request_id':bundle.request_id,'raw_turn_artifact_id':self.publisher.turn_artifact_id(bundle.request_id,attempt)})
                last_error = turn.error or "critic execution error"
                prior_error, prior_response = last_error, None
                continue
            assert turn.content is not None
            canonical = None
            manifest_verified = False
            try:
                if aliases:
                    manifest = verify_manifest(bundle, lease_id, **stores)
                    verify_dispatch(bundle, manifest, self.artifact_store)
                    manifest_verified = True
                    canonical = resolve_alias_response(turn.content, manifest)
                analysis = parse_critic_response(canonical if aliases else turn.content, bundle)
            except Exception as exc:
                if not aliases and not isinstance(exc, CriticProtocolError):
                    raise
                if aliases:
                    if manifest_verified:
                        try:
                            record_resolution(bundle,artifact_store=self.artifact_store,manifest=manifest,raw_content=turn.content,resolved_content=canonical,error=str(exc))
                        except Exception as audit_error:
                            raise CriticConformanceError(str(exc), {'request_id':bundle.request_id,'audit_persistence_error':str(audit_error)}) from exc
                    raise CriticConformanceError(str(exc), {'request_id':bundle.request_id,'run_id':run_id,'todo_id':bundle.todo_id,'lease_id':lease_id,
                        'manifest_fingerprint':manifest.fingerprint,'manifest_verified':manifest_verified,
                        'resolution_audit':'Artifact saved' if manifest_verified else 'Not written into invalid manifest storage; first error persisted by Mission',
                        'raw_turn_artifact_id':self.publisher.turn_artifact_id(bundle.request_id,attempt)}) from exc
                last_error = str(exc)
                prior_error, prior_response = last_error, turn.content
                continue
            if aliases:
                record_resolution(bundle,artifact_store=self.artifact_store,manifest=manifest,raw_content=turn.content,resolved_content=canonical,error=None)
            raw_id = self.publisher.turn_artifact_id(bundle.request_id, attempt)
            report = CriticReport(
                report_id=bundle.request_id,
                request_fingerprint=bundle.request_fingerprint,
                run_id=bundle.run_id,
                todo_id=bundle.todo_id,
                verification_report_id=bundle.verification_report_id,
                candidate_diff_sha256=bundle.candidate_diff_sha256,
                analysis=analysis,
                model_id=turn.model_id or self.policy.model_id,
                model_tag=turn.model_tag or "unknown",
                model_attempts=attempt + 1,
                raw_turn_artifact_id=raw_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.publisher.publish_report(report)
            self._assert_candidate_unchanged(lease_id, baseline)
            return CriticCoordinatorResult(report, False, model_calls, reused_turns)
        raise CriticExhaustedError(f"critic exhausted {self.policy.max_model_attempts} attempts: {last_error}")

    def _route(self, bundle: Any):
        task_type = TaskType.FAILURE_ANALYSIS if bundle.verification_outcome.value == "FAILED" else TaskType.CRITIQUE
        spec = RoutingTaskSpec(
            task_type,
            Complexity.HARD if task_type is TaskType.FAILURE_ANALYSIS else Complexity.NORMAL,
            Scope.REPOSITORY,
            NeedLevel.HIGH,
            NeedLevel.LOW,
            NeedLevel.LOW,
            ContextEstimate(max(1, len(bundle.candidate_diff_text) // 4), ContextBucket.MEDIUM),
            ValidationLevel.HIGH,
            NeedLevel.HIGH if task_type is TaskType.FAILURE_ANALYSIS else NeedLevel.MEDIUM,
            1.0,
        )
        decision = self.router.route(self.factory.build(spec, self.snapshot_provider(), ()))
        if decision.decision_type is not DecisionType.WORKER or decision.selected_model != self.policy.model_id:
            raise CriticRoutingError(f"critic routing must select {self.policy.model_id}: {decision.routing_reason}")
        return decision

    def _assert_candidate_unchanged(self, lease_id: str, baseline: Any) -> None:
        if not self.policy.enforce_no_workspace_mutation:
            return
        current = self.manager.snapshot(lease_id)
        if (
            current.head_commit != baseline.head_commit
            or current.diff_sha256 != baseline.diff_sha256
            or current.dirty_paths != baseline.dirty_paths
            or getattr(current, 'content_sha256', None) != getattr(baseline, 'content_sha256', None)
        ):
            raise CriticIntegrityError("critic execution changed candidate worktree")
