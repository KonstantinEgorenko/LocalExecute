'Request-derived structured-output schemas for recovery model calls.\n\nThese schemas constrain generation only. ``harness.recovery.protocol`` keeps\nexclusive authority over all semantic validation.\n'
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..protocol_transport import ollama_compatible_schema
from .models import FailureEvidenceV2, FailureKind, diagnosis_replan_values
from .protocol import DIAGNOSIS_RESPONSE_SCHEMA, REPLAN_RESPONSE_SCHEMA, diagnosis_response_schema


def constrain_diagnosis_semantics(schema: dict[str, Any]) -> dict[str, Any]:
    """Project existing semantic combinations into complete native object branches."""
    branches = []
    for value in schema["properties"]["failure_kind"]["enum"]:
        branch = deepcopy(schema)
        branch["properties"]["failure_kind"]["enum"] = [value]
        branch["properties"]["replan_required"]["enum"] = list(
            diagnosis_replan_values(FailureKind(value)))
        branches.append(branch)
    return {"oneOf": branches}


def diagnosis_transport_schema(bundle: FailureEvidenceV2) -> dict[str, Any]:
    schema = ollama_compatible_schema(diagnosis_response_schema(bundle))
    properties = schema["properties"]
    properties["evidence"]["items"] = {
        "type": "string",
        "enum": sorted(bundle.allowed_evidence_ids),
    }
    if bundle.workspace_paths:
        properties["implicated_files"]["items"] = {
            "type": "string",
            "enum": sorted(bundle.workspace_paths),
        }
    else:
        properties["implicated_files"]["maxItems"] = 0
    return constrain_diagnosis_semantics(schema)


def replan_transport_schema(
    bundle: FailureEvidenceV2,
    *,
    max_context_tokens: int,
    max_target_files: int,
) -> dict[str, Any]:
    mutable_paths = sorted(set(bundle.workspace_paths) - set(bundle.immutable_paths))
    if not mutable_paths:
        raise ValueError("replan transport requires at least one mutable workspace path")

    schema = ollama_compatible_schema(deepcopy(REPLAN_RESPONSE_SCHEMA))
    properties = schema["properties"]
    properties["source_task_id"] = {"type": "string", "enum": [bundle.task_id]}
    properties["target_files"]["items"] = {
        "type": "string",
        "enum": mutable_paths,
    }
    properties["target_files"]["maxItems"] = min(max_target_files, len(mutable_paths))
    properties["required_evidence"]["items"] = {
        "type": "string",
        "enum": sorted(bundle.allowed_evidence_ids),
    }
    return schema
