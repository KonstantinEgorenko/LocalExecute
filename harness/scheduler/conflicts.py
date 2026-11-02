'Conservative repository intelligence working-set conflict prediction for scheduler.'
from __future__ import annotations

from .models import SchedulerPolicy, SchedulerTodoState


def working_sets_conflict(left: SchedulerTodoState, right: SchedulerTodoState, policy: SchedulerPolicy) -> bool:
    if left.todo_id == right.todo_id:
        return True
    a, b = left.working_set, right.working_set
    if (a.unknown or b.unknown) and policy.serialize_unknown_working_sets:
        return True
    if not policy.allow_parallel_conflict_free:
        return True
    a_paths, b_paths = set(a.paths), set(b.paths)
    if a_paths & b_paths:
        return True
    # Parent/child path overlaps are conservative conflict boundaries too.
    for x in a_paths:
        for y in b_paths:
            if x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/"):
                return True
    return False
