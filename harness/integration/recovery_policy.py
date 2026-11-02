'Production recovery composition for long-horizon workflow workflows.\n\nFrozen recovery/16 policy and controllers remain unchanged.  Production adds:\n- one bounded recovery slot after the configured context-expansion allowance;\n- durable accounting of actual ORNITH_REPLAN actions instead of treating every\n  workflow recovery cycle as a replan.\n'

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..workflow import WorkflowExecutionPolicy


def production_workflow_policy(root: Path) -> WorkflowExecutionPolicy:
    'Reserve one bounded repair cycle after context-expansion recovery.\n\n    Frozen workflow historically allows:\n        max_recovery_cycles_per_todo == max_context_expansions_per_todo == 2\n\n    That permits two context recoveries but leaves no recovery boundary after\n    the first real implementation/verification failure.  Production keeps the\n    frozen context-expansion allowance unchanged and reserves exactly one\n    additional recovery cycle for a subsequent repair decision.\n    '

    base = WorkflowExecutionPolicy.from_project_root(root)

    required_recovery_cycles = (
        base.max_context_expansions_per_todo + 1
    )

    if base.max_recovery_cycles_per_todo >= required_recovery_cycles:
        return base

    return replace(
        base,
        max_recovery_cycles_per_todo=required_recovery_cycles,
    )


class _ActualReplanController:
    """Override only the inaccurate recovery-cycle -> replan accounting."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.actual_replans_used = 0

    def next_action(
        self,
        diagnosis,
        *,
        replans_used: int,
    ):
        # Frozen Phase16RecoveryBridge passes recovery_cycle - 1 here.
        # Production supplies the durable count of successful ORNITH_REPLAN
        # recovery actions instead.
        return self._inner.next_action(
            diagnosis,
            replans_used=self.actual_replans_used,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ProductionRecoveryBridge:
    'Preserve recovery authority while correcting production replan accounting.\n\n    The authoritative count is rebuilt from durable workflow RECOVERY_CONTEXT\n    artifacts, so process restart cannot silently reset the replan budget.\n    '

    def __init__(
        self,
        bridge: Any,
        *,
        artifact_store: Any,
    ) -> None:
        self.bridge = bridge
        self.artifact_store = artifact_store
        self._lock = threading.RLock()

        self._controller = _ActualReplanController(
            bridge.controller
        )
        bridge.controller = self._controller

    def _actual_replans_used(self, todo_id: str) -> int:
        count = 0

        for ref in self.artifact_store.list_refs():
            artifact = self.artifact_store.get_artifact(
                ref.artifact_id
            )

            metadata = artifact.metadata

            if (
                metadata.get("phase16_role") != "RECOVERY_CONTEXT"
                or metadata.get("todo_id") != todo_id
            ):
                continue

            if not isinstance(artifact.content, str):
                raise RuntimeError(
                    "durable Phase16 recovery artifact content "
                    "must be text"
                )

            try:
                payload = json.loads(artifact.content)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "malformed durable Phase16 recovery artifact: "
                    f"{artifact.artifact_id}"
                ) from exc

            if not isinstance(payload, dict):
                raise RuntimeError(
                    "durable Phase16 recovery artifact must "
                    f"contain an object: {artifact.artifact_id}"
                )

            recovery = payload.get("recovery")
            if not isinstance(recovery, dict):
                raise RuntimeError(
                    "durable Phase16 recovery artifact lacks "
                    f"recovery object: {artifact.artifact_id}"
                )

            if recovery.get("controller_action") == "ORNITH_REPLAN":
                count += 1

        return count

    def recover(self, todo, **kwargs):
        # workflow serializes physical production execution, but hold a lock
        # anyway so the temporary controller override cannot leak across Todos.
        with self._lock:
            self._controller.actual_replans_used = (
                self._actual_replans_used(todo.todo_id)
            )

            try:
                return self.bridge.recover(
                    todo,
                    **kwargs,
                )
            finally:
                self._controller.actual_replans_used = 0

# Canonical composition aliases. Historical names remain import-compatible.
production_workflow_policy = production_workflow_policy
ProductionRecoveryBridge = ProductionRecoveryBridge

# Verified compatibility aliases for historical Python interfaces.
production_phase16_policy = production_workflow_policy
ProductionPhase15RecoveryBridge = ProductionRecoveryBridge
