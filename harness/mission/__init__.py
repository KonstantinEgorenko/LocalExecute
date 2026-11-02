'mission durable autonomous mission lifecycle.'
from .controller import DurableMissionController
from .models import (
    DagAmendmentOperationV1,
    DagAmendmentV1,
    DagOperationKind,
    GlobalCompletionGateV1,
    MissionCheckpointV1,
    MissionEventV1,
    MissionReportV1,
    MissionRunResultV1,
    MissionSpecV1,
    MissionState,
    MissionLifecyclePolicy,
    PlanObservationV1,
)
from .workflow_bridge import WorkflowExecutionBridge
from .store import (
    JsonMissionStore,
    MissionAlreadyExistsError,
    MissionCorruptStateError,
    MissionGenerationConflict,
    MissionNotFoundError,
    MissionStoreError,
)

__all__ = [
    "DagAmendmentOperationV1",
    "DagAmendmentV1",
    "DagOperationKind",
    "DurableMissionController",
    "GlobalCompletionGateV1",
    "JsonMissionStore",
    "MissionAlreadyExistsError",
    "MissionCheckpointV1",
    "MissionCorruptStateError",
    "MissionEventV1",
    "MissionGenerationConflict",
    "MissionNotFoundError",
    "MissionReportV1",
    "MissionRunResultV1",
    "MissionSpecV1",
    "MissionState",
    "MissionStoreError",
    "WorkflowExecutionBridge",
    "MissionLifecyclePolicy",
    "PlanObservationV1",
]

# Explicit version-one public compatibility; not canonical exports.
def __getattr__(name):
    from harness.compatibility.public_exports import resolve_legacy_export
    return resolve_legacy_export(__name__, name)
