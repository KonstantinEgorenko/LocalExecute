'execution profiles FINAL-only grace contract and eligibility logic.'
from __future__ import annotations

import json
from dataclasses import dataclass

from harness.executor.models import ExecutionMessage
from harness.workspace.models import ToolStatus
from harness.executor.profiles import ExecutionProfile, ExecutionProfilePolicy
from harness.executor.profile_tool_models import V2ToolFeedback


@dataclass(frozen=True, slots=True)
class FinalizationEligibility:
    eligible: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be bool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty string")


def final_only_json_schema_v2() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "action", "tool_call", "final"],
        "properties": {
            "schema_version": {"type": "integer", "enum": [2]},
            "action": {"type": "string", "enum": ["FINAL"]},
            "tool_call": {"type": "null"},
            "final": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string", "minLength": 1}},
            },
        },
    }


def finalization_message_v2() -> ExecutionMessage:
    return ExecutionMessage(
        "user",
        "FINALIZATION_GRACE_V2\n"
        + json.dumps(
            {
                "tools_available": False,
                "instruction": (
                    "No more productive/tool calls are permitted. Return exactly "
                    "one Tool Protocol V2 FINAL object describing the completed work."
                ),
            },
            sort_keys=True,
        ),
    )


def evaluate_finalization_eligibility(
    *,
    profile: ExecutionProfile,
    policy: ExecutionProfilePolicy,
    productive_calls_used: int,
    total_model_calls_used: int,
    contract_max_worker_calls: int,
    changed_paths: tuple[str, ...],
    last_feedback: V2ToolFeedback | None,
) -> FinalizationEligibility:
    if profile.budget.finalization_grace_calls <= 0:
        return FinalizationEligibility(False, "profile_has_no_finalization_grace")
    if policy.finalization_grace_max_calls <= 0:
        return FinalizationEligibility(False, "policy_disables_finalization_grace")
    if productive_calls_used < profile.budget.max_productive_calls:
        return FinalizationEligibility(False, "productive_budget_not_exhausted")
    if total_model_calls_used >= contract_max_worker_calls:
        return FinalizationEligibility(False, "work_contract_has_no_remaining_worker_call")
    if total_model_calls_used >= profile.budget.max_total_model_calls:
        return FinalizationEligibility(False, "profile_total_model_call_budget_exhausted")
    if policy.require_changed_paths_for_grace and not changed_paths:
        return FinalizationEligibility(False, "no_workspace_mutation_to_finalize")
    if policy.require_successful_terminal_tool_for_grace:
        if last_feedback is None:
            return FinalizationEligibility(False, "no_terminal_tool_feedback")
        if last_feedback.status is not ToolStatus.SUCCESS:
            return FinalizationEligibility(False, "terminal_tool_not_successful")
    return FinalizationEligibility(True, "final_only_grace_allowed")
