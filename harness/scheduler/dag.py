'Deterministic DAG validation/readiness helpers for scheduler.'
from __future__ import annotations

from collections import defaultdict, deque

from .errors import SchedulerPolicyError
from .models import SchedulerTodoState, SchedulerTodoStatus


_FAILURE_DEPENDENCY = {
    SchedulerTodoStatus.FAILED,
    SchedulerTodoStatus.BLOCKED,
    SchedulerTodoStatus.HUMAN_REVIEW,
    SchedulerTodoStatus.CANCELLED,
    SchedulerTodoStatus.REPLAN_REQUIRED,
}


def validate_dag(states: tuple[SchedulerTodoState, ...]) -> None:
    if not states:
        raise SchedulerPolicyError("scheduler DAG must contain at least one Todo")
    by_id = {state.todo_id: state for state in states}
    if len(by_id) != len(states):
        raise SchedulerPolicyError("scheduler DAG contains duplicate todo_id")
    goals = {state.goal_id for state in states}
    if len(goals) != 1:
        raise SchedulerPolicyError("scheduler DAG must belong to exactly one goal")
    for state in states:
        for dep in state.dependencies:
            if dep == state.todo_id:
                raise SchedulerPolicyError(f"Todo {state.todo_id} cannot depend on itself")
            if dep not in by_id:
                raise SchedulerPolicyError(f"Todo {state.todo_id} references unknown dependency {dep}")
    indegree = {todo_id: 0 for todo_id in by_id}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for state in states:
        for dep in state.dependencies:
            indegree[state.todo_id] += 1
            outgoing[dep].append(state.todo_id)
    ready = deque(sorted((x for x, degree in indegree.items() if degree == 0), key=str.casefold))
    seen = 0
    while ready:
        current = ready.popleft(); seen += 1
        for nxt in sorted(outgoing[current], key=str.casefold):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if seen != len(states):
        raise SchedulerPolicyError("scheduler DAG contains dependency cycle")


def derived_readiness(state: SchedulerTodoState, states: dict[str, SchedulerTodoState]) -> tuple[SchedulerTodoStatus, str | None]:
    if state.status not in {SchedulerTodoStatus.PENDING, SchedulerTodoStatus.READY}:
        return state.status, state.blocked_reason
    dep_states = [states[dep].status for dep in state.dependencies]
    failed = [dep for dep in state.dependencies if states[dep].status in _FAILURE_DEPENDENCY]
    if failed:
        return SchedulerTodoStatus.BLOCKED, "dependency_failed:" + ",".join(sorted(failed, key=str.casefold))
    if all(status is SchedulerTodoStatus.SUCCEEDED for status in dep_states):
        return SchedulerTodoStatus.READY, None
    return SchedulerTodoStatus.PENDING, None
