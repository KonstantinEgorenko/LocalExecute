'scheduler deterministic Todo scheduler over durable claims, leases, and Judge decisions.'
from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from ..control_plane import Evidence, EvidenceStatus, Todo
from ..control_plane.store import RunStore
from ..judge.models import JudgeAction, JudgeDecision, JudgeReport
from ..repo_intelligence.models import RepositoryRetrievalResult
from ..worktree.manager import GitWorktreeManager
from ..worktree.models import WorktreeLease
from .conflicts import working_sets_conflict
from .errors import SchedulerConflictError, SchedulerStateError
from .models import (
    SchedulerClaim,
    SchedulerDecisionResult,
    SchedulerLease,
    SchedulerPolicy,
    SchedulerTodoState,
    SchedulerTodoStatus,
    WorkingSetPrediction,
)
from .store import FileSchedulerStore


class SchedulerCoordinator:
    """Deterministically decides WHAT may execute now; it does not invoke models/workers itself."""

    def __init__(
        self,
        *,
        store: FileSchedulerStore,
        policy: SchedulerPolicy | None = None,
        run_store: RunStore | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or SchedulerPolicy()
        self.run_store = run_store
        self.clock = clock or (lambda: int(time.time()))

    def register_goal(self, todos: tuple[Todo, ...], *, run_ids: Mapping[str, str]) -> tuple[SchedulerTodoState, ...]:
        if not todos:
            raise SchedulerStateError("cannot register empty Todo DAG")
        states: list[SchedulerTodoState] = []
        for todo in todos:
            run_id = run_ids.get(todo.todo_id)
            if not isinstance(run_id, str) or not run_id.strip():
                raise SchedulerStateError(f"missing run_id for Todo {todo.todo_id}")
            states.append(SchedulerTodoState(todo.todo_id, todo.goal_id, run_id, todo.objective, todo.dependencies, todo.priority))
        return self.store.register(tuple(states))

    def bind_retrieval(self, todo_id: str, retrieval: RepositoryRetrievalResult) -> SchedulerTodoState:
        if not isinstance(retrieval, RepositoryRetrievalResult):
            raise TypeError("retrieval must be RepositoryRetrievalResult")
        prediction = WorkingSetPrediction(retrieval.retrieval_id, tuple(hit.path for hit in retrieval.hits), not bool(retrieval.hits))
        return self.store.set_working_set(todo_id, prediction)

    def recover(self) -> tuple[str, ...]:
        now = self.clock()
        expired = self.store.expire_stale(now=now)
        goals = sorted({state.goal_id for state in self.store.list_todos()}, key=str.casefold)
        for goal_id in goals:
            self.store.refresh_readiness(goal_id)
        return expired

    def plan_batch(self, goal_id: str, *, limit: int | None = None) -> tuple[SchedulerTodoState, ...]:
        now = self.clock(); self.store.expire_stale(now=now); states = self.store.refresh_readiness(goal_id)
        maximum = self.policy.max_parallel_todos if limit is None else min(self.policy.max_parallel_todos, max(0, int(limit)))
        if maximum <= 0:
            return ()
        active_ids = {claim.todo_id for claim in self.store.active_claims(now=now)}
        # Global scheduler parallelism/conflict boundary: an active Todo from another
        # goal still consumes capacity and can overlap the same repository paths.
        active = [state for state in self.store.list_todos() if state.todo_id in active_ids]
        ready = [state for state in states if state.status is SchedulerTodoStatus.READY]
        chosen: list[SchedulerTodoState] = []
        available_slots = max(0, self.policy.max_parallel_todos - len(active))
        maximum = min(maximum, available_slots)
        for candidate in ready:
            if len(chosen) >= maximum:
                break
            if any(working_sets_conflict(candidate, other, self.policy) for other in active):
                continue
            if any(working_sets_conflict(candidate, other, self.policy) for other in chosen):
                continue
            chosen.append(candidate)
        return tuple(chosen)

    def claim_next(self, goal_id: str, *, owner_id: str) -> SchedulerClaim | None:
        batch = self.plan_batch(goal_id, limit=1)
        if not batch:
            return None
        return self.store.claim(
            batch[0].todo_id,
            owner_id,
            now=self.clock(),
            ttl_seconds=self.policy.claim_ttl_seconds,
            max_attempts=self.policy.max_attempts_per_todo,
            max_active_claims=self.policy.max_active_claims,
        )

    def claim_batch(self, goal_id: str, *, owner_ids: tuple[str, ...]) -> tuple[SchedulerClaim, ...]:
        if not owner_ids:
            return ()
        candidates = self.plan_batch(goal_id, limit=len(owner_ids))
        claims: list[SchedulerClaim] = []
        for state, owner in zip(candidates, owner_ids, strict=False):
            claims.append(self.store.claim(
                state.todo_id, owner, now=self.clock(), ttl_seconds=self.policy.claim_ttl_seconds,
                max_attempts=self.policy.max_attempts_per_todo, max_active_claims=self.policy.max_active_claims,
            ))
        return tuple(claims)

    def heartbeat(self, claim_id: str, *, owner_id: str) -> SchedulerClaim:
        now = self.clock()
        claim = self.store.renew_claim(claim_id, owner_id=owner_id, now=now, ttl_seconds=self.policy.claim_ttl_seconds)
        for lease in self.store.active_leases(now=now):
            if lease.claim_id == claim_id:
                self.store.renew_lease(lease.lease_id, owner_id=owner_id, now=now, ttl_seconds=self.policy.resource_lease_ttl_seconds)
        return claim

    def start(self, claim_id: str) -> SchedulerTodoState:
        return self.store.start(claim_id, now=self.clock())

    def wait_decision(self, claim_id: str) -> SchedulerTodoState:
        return self.store.wait_decision(claim_id, now=self.clock())

    def acquire_resource(self, claim_id: str, *, resource_key: str, owner_id: str) -> SchedulerLease:
        return self.store.acquire_lease(
            claim_id=claim_id, resource_key=resource_key, owner_id=owner_id,
            now=self.clock(), ttl_seconds=self.policy.resource_lease_ttl_seconds,
        )

    def allocate_worktree(
        self,
        claim_id: str,
        *,
        manager: GitWorktreeManager,
        source_repo_root: str,
        base_ref: str = "HEAD",
    ) -> WorktreeLease:
        claim = self.store.get_claim(claim_id)
        if claim.status.value != "ACTIVE" or claim.expires_at <= self.clock():
            raise SchedulerStateError("active scheduler claim required for worktree allocation")
        state = self.store.get_todo(claim.todo_id)
        lease = manager.acquire(source_repo_root=source_repo_root, run_id=state.run_id, todo_id=state.todo_id, base_ref=base_ref)
        self.store.attach_worktree(claim_id, lease.lease_id, now=self.clock())
        return lease

    def apply_judge(self, report: JudgeReport) -> SchedulerDecisionResult:
        if not isinstance(report, JudgeReport):
            raise TypeError("report must be JudgeReport")
        state = self.store.get_todo(report.todo_id)
        if state.run_id != report.run_id:
            raise SchedulerStateError("JudgeReport run_id does not match scheduler Todo")
        if state.last_judge_report_id == report.report_id and state.active_claim_id is None:
            # Crash-safe replay: the scheduler transition may have committed before the
            # caller observed success or before Control Plane evidence was recorded.
            self._record_control_plane(report, state)
            return SchedulerDecisionResult(state.todo_id, SchedulerTodoStatus.WAITING_DECISION, state.status, report.analysis.decision, report.analysis.next_action, state.attempt_count)
        if state.status is not SchedulerTodoStatus.WAITING_DECISION or state.active_claim_id is None:
            raise SchedulerStateError("Todo must be WAITING_DECISION before applying JudgeReport")
        previous = state.status
        decision, action = report.analysis.decision, report.analysis.next_action
        blocked_reason: str | None = None
        if decision is JudgeDecision.ACCEPT:
            destination = SchedulerTodoStatus.SUCCEEDED
        elif decision in {JudgeDecision.RETRY, JudgeDecision.ESCALATE}:
            if state.attempt_count >= self.policy.max_attempts_per_todo:
                destination = SchedulerTodoStatus.FAILED
                blocked_reason = "attempt_quota_exhausted"
            else:
                destination = SchedulerTodoStatus.READY
        elif decision is JudgeDecision.REPLAN:
            destination = SchedulerTodoStatus.REPLAN_REQUIRED
            blocked_reason = "judge_replan"
        elif decision is JudgeDecision.BLOCKED:
            destination = SchedulerTodoStatus.BLOCKED
            blocked_reason = "judge_blocked"
        elif decision is JudgeDecision.HUMAN_REVIEW:
            destination = SchedulerTodoStatus.HUMAN_REVIEW
            blocked_reason = "judge_human_review"
        else:  # defensive for future enum extension
            raise SchedulerStateError(f"unsupported Judge decision {decision}")
        updated = self.store.finish_claim(
            claim_id=state.active_claim_id, now=self.clock(), status=destination,
            last_judge_report_id=report.report_id, next_action=action, blocked_reason=blocked_reason,
        )
        self._record_control_plane(report, updated)
        self.store.refresh_readiness(updated.goal_id)
        return SchedulerDecisionResult(updated.todo_id, previous, updated.status, decision, action, updated.attempt_count)

    def _record_control_plane(self, report: JudgeReport, state: SchedulerTodoState) -> None:
        if self.run_store is None:
            return
        evidence_id = f"{report.run_id}:scheduler:{report.report_id}"
        existing = [event.to_dict()["payload"] for event in self.run_store.list_events(report.run_id) if event.event_type.value == "EVIDENCE_RECORDED"]
        if any(payload.get("evidence_id") == evidence_id for payload in existing):
            return
        self.run_store.record_evidence(
            report.run_id,
            Evidence(
                evidence_id, report.todo_id, "SCHEDULER_DECISION", "Phase11Scheduler", EvidenceStatus.PASSED,
                {
                    "judge_report_id": report.report_id,
                    "decision": report.analysis.decision.value,
                    "next_action": report.analysis.next_action.value,
                    "scheduler_status": state.status.value,
                    "attempt_count": state.attempt_count,
                },
            ),
        )
