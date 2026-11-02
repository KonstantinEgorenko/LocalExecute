'workflow autonomous workflow orchestrator.\n\nThis controller only coordinates frozen subsystem ports. It owns no model\nselection policy, no workspace mutation authority, no verification truth, and\nno scheduler state. Its new responsibility is long-horizon continuity: every\nattempt receives a model-sized view of durable external context, while verified\noutcomes are promoted back into that external context for later Todos.\n'
from __future__ import annotations

from ..context_manager.errors import ContextBudgetError, ContextResolutionError
from .context_spine import ExternalContextSpine
from .models import (
    WorkflowExecutionPolicy,
    RecoveryObservationV1,
    VerificationObservationV1,
    VerifiedTodoOutcomeV1,
    WorkflowOutcome,
    WorkflowResultV1,
    WorkflowTodoV1,
    canonical_json,
)
from .ports import (
    ExecutionPort,
    FinalSynthesisPort,
    RecoveryPort,
    RepositoryContextPort,
    ReviewPort,
    RouterPort,
    SchedulerPort,
    SnapshotPort,
)


class AutonomousWorkflowController:
    def __init__(
        self,
        *,
        policy: WorkflowExecutionPolicy,
        spine: ExternalContextSpine,
        scheduler: SchedulerPort,
        repository: RepositoryContextPort,
        router: RouterPort,
        executor: ExecutionPort,
        verifier: VerificationPort,
        reviewer: ReviewPort,
        recovery: RecoveryPort,
        snapshots: SnapshotPort,
        final_synthesis: FinalSynthesisPort,
    ) -> None:
        self.policy = policy
        self.spine = spine
        self.scheduler = scheduler
        self.repository = repository
        self.router = router
        self.executor = executor
        self.verifier = verifier
        self.reviewer = reviewer
        self.recovery = recovery
        self.snapshots = snapshots
        self.final_synthesis = final_synthesis

    def initialize_context(
        self,
        *,
        goal_id: str,
        raw_objective: str,
        goal_capsule: str,
        global_constraints: dict[str, object] | None = None,
    ) -> None:
        self.spine.publish_root_context(
            goal_id=goal_id,
            raw_objective=raw_objective,
            goal_capsule=goal_capsule,
            global_constraints=global_constraints,
        )
        try:
            self.spine.latest_project_snapshot(goal_id)
        except ContextResolutionError:
            initial = self.snapshots.current(goal_id=goal_id, epoch=0)
            if initial.epoch != 0:
                raise ValueError("initial project snapshot must use epoch=0")
            self.spine.publish_project_snapshot(initial)

    def _resume_committed_success(self, goal_id: str, todo: WorkflowTodoV1) -> bool:
        """Finish a scheduler transition after a crash without re-running code.

        Context artifacts are published before the scheduler success transition.
        If a crash happens in that narrow window, a later run observes both the
        verified outcome and the matching project epoch and commits only the
        scheduler transition. It never invokes the worker again.
        """
        if hasattr(self.scheduler,'resume_success'):
            resumed=self.scheduler.resume_success(todo)
            if resumed is not None:
                return resumed
        try:
            outcome, ref = self.spine.verified_outcome(goal_id, todo.todo_id)
            snapshot, _ = self.spine.latest_project_snapshot(goal_id)
        except ContextResolutionError:
            return False
        if snapshot.epoch < outcome.epoch_after:
            return False
        verification = VerificationObservationV1(
            True,
            outcome.verification_report_id,
            outcome.verification_fingerprint,
            outcome.summary,
            outcome.evidence_ids,
            {"phase16_restart_reuse": True},
        )
        self.scheduler.mark_succeeded(todo, verification, ref.artifact_id)
        return True

    def _publish_success(
        self,
        *,
        goal_id: str,
        todo: WorkflowTodoV1,
        objective: str,
        execution,
        verification: VerificationObservationV1,
        review,
    ) -> str:
        actual_run_id = (self.scheduler.prepare_success(todo,verification,review)
                         if hasattr(self.scheduler,'prepare_success') else None)
        before, _ = self.spine.latest_project_snapshot(goal_id)
        after_epoch = before.epoch + 1
        after = self.snapshots.current(goal_id=goal_id, epoch=after_epoch)
        if after.epoch != after_epoch:
            raise ValueError("SnapshotPort returned wrong epoch")
        summary = review.summary or execution.summary or verification.summary
        outcome = VerifiedTodoOutcomeV1(
            goal_id=goal_id,
            todo_id=todo.todo_id,
            run_id=actual_run_id or todo.run_id,
            objective=objective,
            summary=summary,
            epoch_before=before.epoch,
            epoch_after=after_epoch,
            changed_paths=execution.changed_paths,
            verification_report_id=verification.report_id,
            verification_fingerprint=verification.fingerprint,
            invariants=(),
            evidence_ids=tuple(dict.fromkeys((*verification.evidence_ids, *review.evidence_ids))),
        )
        outcome_ref = self.spine.publish_verified_outcome(outcome)
        # Publish the new snapshot after the verified outcome but before scheduler
        # success. _resume_committed_success makes this ordering crash-safe.
        self.spine.publish_project_snapshot(after)
        if hasattr(self.scheduler,'publish_success'):
            self.scheduler.publish_success(todo,after,outcome_ref)
        self.scheduler.mark_succeeded(todo, verification, outcome_ref.artifact_id)
        return outcome_ref.artifact_id

    def run(
        self,
        *,
        goal_id: str,
        raw_objective: str,
        goal_capsule: str,
        global_constraints: dict[str, object] | None = None,
    ) -> WorkflowResultV1:
        self.initialize_context(
            goal_id=goal_id,
            raw_objective=raw_objective,
            goal_capsule=goal_capsule,
            global_constraints=global_constraints,
        )
        steps = 0
        recovery_cycles_total = 0
        context_packs = 0
        completed: list[str] = []

        while steps < self.policy.max_workflow_steps:
            if self.scheduler.all_succeeded(goal_id):
                final = self.final_synthesis.synthesize(
                    goal_id=goal_id,
                    completed_todo_ids=tuple(completed),
                )
                return WorkflowResultV1(
                    WorkflowOutcome.SUCCEEDED,
                    goal_id,
                    tuple(completed),
                    None,
                    steps,
                    recovery_cycles_total,
                    context_packs,
                    final,
                )

            pending_replan = (
                self.scheduler.pending_replan_todo_id(goal_id)
                if hasattr(self.scheduler, "pending_replan_todo_id") else None
            )
            if pending_replan is not None:
                if hasattr(self.scheduler, "ensure_pending_replan_required"):
                    self.scheduler.ensure_pending_replan_required(goal_id, pending_replan)
                return WorkflowResultV1(
                    WorkflowOutcome.NEEDS_ATTENTION, goal_id, tuple(completed),
                    pending_replan, steps, recovery_cycles_total, context_packs,
                    f"semantic_replan_required:{pending_replan}",
                )

            ready = self.scheduler.ready(goal_id, limit=1 if self.policy.serialize_gpu_execution else 2)
            if not ready:
                reason = self.scheduler.terminal_reason(goal_id)
                return WorkflowResultV1(
                    WorkflowOutcome.BLOCKED if reason else WorkflowOutcome.NEEDS_ATTENTION,
                    goal_id,
                    tuple(completed),
                    None,
                    steps,
                    recovery_cycles_total,
                    context_packs,
                    reason or "scheduler has no READY Todo but workflow is incomplete",
                )

            todo = ready[0]
            if self._resume_committed_success(goal_id, todo):
                if todo.todo_id not in completed:
                    completed.append(todo.todo_id)
                continue

            self.scheduler.begin(todo)
            current_objective = todo.objective
            current_routing_task_spec = todo.routing_task_spec
            current_expansion_queries: tuple[str, ...] = ()
            failed_profiles: list[str] = []
            recovery_refs = []
            todo_succeeded = False
            terminal_reason: str | None = None

            for recovery_cycle in range(self.policy.max_recovery_cycles_per_todo + 1):
                active_todo = WorkflowTodoV1(
                    todo.todo_id,
                    todo.run_id,
                    current_objective,
                    todo.dependencies,
                    todo.work_contract,
                    current_routing_task_spec,
                )
                repository = self.repository.retrieve(
                    active_todo,
                    objective=current_objective,
                    expansion_queries=current_expansion_queries,
                )
                selection = self.router.select(
                    active_todo,
                    repository,
                    objective=current_objective,
                    failed_profile_ids=tuple(failed_profiles),
                )
                profile_chain = (selection.profile_id, *selection.cascade_profile_ids)
                last_execution = None
                last_verification = None
                review_for_recovery = None

                for chain_index, profile_id in enumerate(profile_chain):
                    try:
                        pack = self.spine.build_pack(
                            goal_id=goal_id,
                            todo_id=todo.todo_id,
                            objective=current_objective,
                            contract=todo.work_contract,
                            profile_id=profile_id,
                            dependency_todo_ids=todo.dependencies,
                            optional_repo_artifacts=repository.artifact_refs,
                            recovery_artifacts=tuple(recovery_refs),
                        )
                    except ContextBudgetError as exc:
                        # A context profile is only a bounded projection choice.
                        # If required durable context cannot fit, preserve the
                        # hard budget and deterministically cascade to the next
                        # configured profile instead of failing the mission
                        # boundary before worker execution.
                        if profile_id not in failed_profiles:
                            failed_profiles.append(profile_id)
                        terminal_reason = (
                            f"context pack failed after profile={profile_id}: {exc}"
                        )
                        if chain_index + 1 < len(profile_chain):
                            continue
                        break
                    context_packs += 1
                    steps += 1
                    if steps > self.policy.max_workflow_steps:
                        terminal_reason = "max_workflow_steps exhausted"
                        break
                    execution = self.executor.execute(
                        active_todo,
                        profile_id=profile_id,
                        objective_with_context=self.spine.objective_with_pack(current_objective, pack),
                        attempt_index=steps,
                        fresh_workspace=recovery_cycle > 0,
                    )
                    verification = self.verifier.verify(active_todo, execution)
                    last_execution, last_verification = execution, verification
                    if verification.passed:
                        review = self.reviewer.review(active_todo, execution, verification)
                        if review.accept:
                            self._publish_success(
                                goal_id=goal_id,
                                todo=todo,
                                objective=current_objective,
                                execution=execution,
                                verification=verification,
                                review=review,
                            )
                            todo_succeeded = True
                            if todo.todo_id not in completed:
                                completed.append(todo.todo_id)
                            break
                        review_for_recovery = review
                        terminal_reason = f"review decision={review.decision}: {review.summary}"
                        break
                    if profile_id not in failed_profiles:
                        failed_profiles.append(profile_id)
                    terminal_reason = f"verification failed after profile={profile_id}: {verification.summary}"
                    if chain_index + 1 < len(profile_chain):
                        continue
                    break

                if todo_succeeded:
                    break
                if steps >= self.policy.max_workflow_steps:
                    break
                if last_execution is None or last_verification is None:
                    break
                if (
                    recovery_cycle >= self.policy.max_recovery_cycles_per_todo
                    and not (review_for_recovery is not None and review_for_recovery.decision == "REPLAN")
                ):
                    terminal_reason = terminal_reason or "recovery cycle quota exhausted"
                    break

                recovery_cycle_no = recovery_cycle + 1
                recovery = self.recovery.recover(
                    active_todo,
                    objective=current_objective,
                    execution=last_execution,
                    verification=last_verification,
                    failed_profile_ids=tuple(failed_profiles),
                    recovery_cycle=recovery_cycle_no,
                )
                recovery_cycles_total += 1
                if recovery.raw.get("semantic_replan_required") is True:
                    self.spine.publish_recovery_context(
                        goal_id=goal_id,
                        todo_id=todo.todo_id,
                        cycle=recovery_cycle_no,
                        content=canonical_json({
                            "schema_version": 1,
                            "todo_id": todo.todo_id,
                            "cycle": recovery_cycle_no,
                            "reason": recovery.reason,
                            "recovery": recovery.raw,
                            "review": None if review_for_recovery is None else review_for_recovery.raw,
                            "verification": last_verification.raw,
                        }),
                    )
                    self.scheduler.mark_retryable(todo, recovery.reason)
                    return WorkflowResultV1(
                        WorkflowOutcome.NEEDS_ATTENTION,
                        goal_id, tuple(completed), todo.todo_id, steps,
                        recovery_cycles_total, context_packs,
                        f"semantic_replan_required:{todo.todo_id}",
                    )
                if not recovery.continue_work:
                    terminal_reason = recovery.reason
                    break
                assert recovery.revised_objective is not None
                current_objective = recovery.revised_objective
                if recovery.routing_task_spec is not None:
                    current_routing_task_spec = recovery.routing_task_spec
                current_expansion_queries = recovery.expansion_queries
                recovery_payload = canonical_json({
                    "schema_version": 1,
                    "todo_id": todo.todo_id,
                    "cycle": recovery_cycle_no,
                    "reason": recovery.reason,
                    "revised_objective": recovery.revised_objective,
                    "source_context_artifact_ids": list(recovery.context_artifact_ids),
                    "expansion_queries": list(recovery.expansion_queries),
                    "routing_reclassified": recovery.routing_task_spec is not None,
                    "recovery": recovery.raw,
                    "review": None if review_for_recovery is None else review_for_recovery.raw,
                    "verification": last_verification.raw,
                })
                recovery_ref = self.spine.publish_recovery_context(
                    goal_id=goal_id,
                    todo_id=todo.todo_id,
                    cycle=recovery_cycle_no,
                    content=recovery_payload,
                )
                recovery_refs.append(recovery_ref)
                for artifact_id in recovery.context_artifact_ids:
                    if self.spine.artifact_store.has_artifact(artifact_id):
                        ref = self.spine.artifact_store.get_ref(artifact_id)
                        if ref not in recovery_refs:
                            recovery_refs.append(ref)

            if todo_succeeded:
                continue
            reason = terminal_reason or "Todo failed without recoverable path"
            self.scheduler.mark_failed(todo, reason)
            return WorkflowResultV1(
                WorkflowOutcome.QUOTA_EXHAUSTED if "quota" in reason.casefold() or "max_workflow" in reason else WorkflowOutcome.NEEDS_ATTENTION,
                goal_id,
                tuple(completed),
                todo.todo_id,
                steps,
                recovery_cycles_total,
                context_packs,
                reason,
            )

        return WorkflowResultV1(
            WorkflowOutcome.QUOTA_EXHAUSTED,
            goal_id,
            tuple(completed),
            None,
            steps,
            recovery_cycles_total,
            context_packs,
            "max_workflow_steps exhausted",
        )
