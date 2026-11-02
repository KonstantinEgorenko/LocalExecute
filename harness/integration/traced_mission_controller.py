'Post-frozen lifecycle observation around the authoritative mission controller.'
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..mission import DurableMissionController, MissionState
from ..mission.models import MissionSpecV1
from .mission_flight_recorder import MissionFlightRecorder
from .mission_trace_observer import MissionTraceRegistry


class TracedDurableMissionController(DurableMissionController):
    """Observe public controller boundaries while leaving ``super`` authoritative."""

    def __init__(self, *, trace_state_root: Path, registry: MissionTraceRegistry | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.trace_registry = registry or MissionTraceRegistry(Path(trace_state_root))
        self.registry = self.trace_registry

    def _recorder_for_spec(self, spec: MissionSpecV1) -> MissionFlightRecorder:
        recorder = self.registry.get(spec.mission_id, spec.goal_id)
        recorder.register_request_secrets(spec.to_dict())
        return recorder

    def _recorder_for_mission(self, mission_id: str) -> MissionFlightRecorder:
        return self._recorder_for_spec(self.store.load(mission_id).spec)

    def create(self, spec: MissionSpecV1):
        recorder = self._recorder_for_spec(spec)
        recorder.record("MISSION_REQUEST_RECEIVED", payload={"mission_id": spec.mission_id})
        try:
            checkpoint = super().create(spec)
        except Exception as exc:
            recorder.record("MISSION_FAILED", payload={"exception_type": type(exc).__name__, "message": str(exc)})
            raise
        recorder.record("MISSION_CREATED", payload={"generation": checkpoint.generation})
        return checkpoint

    def plan(self, mission_id: str):
        recorder = self._recorder_for_mission(mission_id)
        recorder.record("MISSION_PLANNING_STARTED")
        try:
            checkpoint = super().plan(mission_id)
        except Exception as exc:
            recorder.record("MISSION_FAILED", payload={"exception_type": type(exc).__name__, "message": str(exc)})
            raise
        recorder.record("MISSION_PLANNED", payload={"state": checkpoint.state.value, "generation": checkpoint.generation})
        return checkpoint

    def run(self, mission_id: str):
        recorder = self._recorder_for_mission(mission_id)
        recorder.record("MISSION_EXECUTION_STARTED")
        try:
            result = super().run(mission_id)
        except Exception as exc:
            recorder.record("MISSION_FAILED", payload={"exception_type": type(exc).__name__, "message": str(exc)})
            recorder.seal(status=MissionState.FAILED.value, terminal_reason=str(exc))
            raise
        event_type = {
            MissionState.COMPLETED: "MISSION_COMPLETED",
            MissionState.BLOCKED: "MISSION_BLOCKED",
            MissionState.FAILED: "MISSION_FAILED",
        }.get(result.state, "MISSION_EXECUTION_FINISHED")
        recorder.record(event_type, payload={"state": result.state.value, "reason": result.reason})
        if result.state in {MissionState.COMPLETED, MissionState.BLOCKED, MissionState.FAILED}:
            recorder.seal(status=result.state.value, terminal_reason=result.reason)
        return result
