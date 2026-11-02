"""Opaque Todo identities shared by production producers and DAG references.

Todo IDs are JSON identities, never filesystem components. Production appends
``:todo:NN`` to the goal identity; historical simple IDs remain valid.
Mission/run/amendment filesystem identifiers have separate contracts.
"""
from __future__ import annotations

import re


_TODO_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)*")
MAX_TODO_ID_LENGTH = 128


def validate_todo_id(value: object, name: str = "todo_id") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty string")
    if len(value) > MAX_TODO_ID_LENGTH or not _TODO_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded canonical Todo identity")
    return value


def make_todo_id(goal_id: str, index: int) -> str:
    """Preserve the existing goal:todo:NN format without lossy normalization."""
    if type(index) is not int or index < 1:
        raise ValueError("Todo index must be a positive integer")
    return validate_todo_id(f"{goal_id}:todo:{index:02d}")
