'Evidence-bound prompts for recovery diagnosis and replanning.'
from __future__ import annotations

from ..executor.models import ExecutionMessage
from .models import FailureDiagnosisV2, FailureEvidenceV2, RecoveryPolicy, canonical_json
from .protocol import DIAGNOSIS_RESPONSE_SCHEMA, REPLAN_RESPONSE_SCHEMA, diagnosis_response_schema, diagnosis_retry_feedback


DIAGNOSIS_SYSTEM = """You are the Phase 15 Ministral Failure Diagnostician in a coding swarm.
Deterministic verification is immutable truth. You have NO tools and MUST NOT modify files.
Diagnose WHY the implementation attempts failed using only supplied evidence IDs and payloads.
Prefer a specific semantic root cause over generic statements. Distinguish an API/representation
contract error from missing context, tool protocol, and infrastructure. corrected_invariants must
state the semantic facts a new implementation must satisfy, not a code patch. Cite only exact
allowed_evidence_ids. A failed deterministic verification can never be accepted. Return JSON only."""

REPLAN_SYSTEM = """You are the Phase 15 Ornith Replanner. You have NO tools and MUST NOT modify files.
Transform the evidence-bound diagnosis into the smallest fresh executable coding task. You do NOT
select a model or execution profile; Router V2 owns that decision. Do not preserve the original
'hard' task class merely because the original attempt was hard: if the diagnosis localizes the root
cause to one known source file, reclassify to single_file_bug or simple_edit with low navigation and
only that source file. The revised_objective must explicitly encode the corrected semantic invariant,
what behavior must remain unchanged, and that tests are immutable. Never target tests. Do not emit a
code patch. Carry forward exact evidence IDs. Return JSON only."""


def diagnosis_messages(
    bundle: FailureEvidenceV2,
    policy: RecoveryPolicy,
    *,
    prior_error: str | None = None,
    prior_response: str | None = None,
) -> tuple[ExecutionMessage, ...]:
    payload = {
        "task": bundle.to_dict(),
        "response_contract": diagnosis_response_schema(bundle),
        "rules": {
            "deterministic_verification_is_truth": True,
            "workspace_mutation_forbidden": True,
            "max_evidence_items": policy.max_evidence_items,
            "implicated_files": "Unique authorized repository-relative paths only. Existing canonical identity replaces backslashes with slashes and normalizes dot/repeated-slash components. No parent traversal, absolute paths or drive prefixes. Equivalent paths count as duplicates; list each canonical path once.",
            "allowed_implicated_files": list(bundle.workspace_paths),
            "can_override_deterministic": False,
        },
    }
    if 'diagnosis_path_domain' in bundle.source_metadata:
        def facts(value):
            # Integrity identities remain in the durable bundle; they are not
            # semantic facts. Preserve full evidence IDs for exact citations.
            if isinstance(value, dict):
                return {key: facts(item) for key, item in value.items()
                        if key not in ('fingerprint', 'report_id')}
            if isinstance(value, list):
                return [facts(item) for item in value]
            return value
        payload['task'] = facts(payload['task'])
        payload['grounding_contract'] = {
            'corrected_invariants': 'Each statement must cite supporting evidence_ids; citations are not chain-of-thought.',
            'allowed_support': ['objective', 'explicit task constraints', 'execution facts',
                                'deterministic gate evidence', 'authorized path metadata'],
            'immutable_paths': 'MUST NOT BE MODIFIED; not positive edit authority. Only mutable_workspace_paths grant task edit scope.',
            'observed_relevant_paths': 'Authorized path identity only; no fresh content assertion.',
            'changed_paths': 'Observed mutation metadata; absence does not prove business/API semantics.',
            'fingerprints': 'Bundle SHA identifies serialized evidence; verification SHA identifies a different report. They are not comparable expected/actual values. Never derive a mismatch or corrected invariant from these hashes or report-ID suffixes. Cite evidence IDs instead.',
            'EXECUTION_NOT_VERIFIABLE': 'Worker did not finish; no actual Phase7 business/syntax/pytest result exists unless separate gate evidence says so.',
            'semantic_failure_kind': 'API_SEMANTICS/TEST_SEMANTICS require source, Judge or actual deterministic verification support; missing changed_paths and non-completion alone are insufficient.',
        }
    if prior_error is not None:
        payload["protocol_retry"] = {
            "violation": diagnosis_retry_feedback(prior_response or "",bundle),
            "error": prior_error.encode("utf-8")[:2048].decode("utf-8", "ignore"),
            "previous_response": "" if prior_response is None else prior_response.encode("utf-8")[:policy.max_response_bytes].decode("utf-8", "ignore"),
            "allowed_implicated_files": list(bundle.workspace_paths),
            "instruction": "Produce a fresh JSON response. implicated_files must contain unique canonical authorized paths; remove duplicate representations in your new response, never invent or substitute paths/evidence IDs.",
        }
    return (
        ExecutionMessage("system", DIAGNOSIS_SYSTEM),
        ExecutionMessage("user", "FAILURE_DIAGNOSIS_REQUEST\n" + canonical_json(payload)),
    )


def replan_prompts(
    bundle: FailureEvidenceV2,
    diagnosis: FailureDiagnosisV2,
    policy: RecoveryPolicy,
    *,
    prior_error: str | None = None,
    prior_response: str | None = None,
) -> tuple[str, str]:
    payload = {
        "task_id": bundle.task_id,
        "source_objective": bundle.objective,
        "source_task_class": bundle.source_task_class.value,
        "source_difficulty": bundle.source_difficulty.value,
        "workspace_paths": list(bundle.workspace_paths),
        "immutable_paths": list(bundle.immutable_paths),
        "allowed_evidence_ids": sorted(bundle.allowed_evidence_ids),
        "diagnosis": diagnosis.to_dict(),
        "response_contract": REPLAN_RESPONSE_SCHEMA,
        "policy": {
            "max_context_tokens": policy.max_context_tokens,
            "max_target_files": policy.max_target_files,
            "fresh_workspace_required": True,
            "preserve_tests": True,
            "router_owns_profile_selection": True,
        },
    }
    if prior_error is not None:
        payload["protocol_retry"] = {
            "error": prior_error,
            "previous_response": "" if prior_response is None else prior_response.encode("utf-8")[:policy.max_response_bytes].decode("utf-8", "ignore"),
            "instruction": "Correct only the replan JSON. Do not select a model and never target immutable tests.",
        }
    return REPLAN_SYSTEM, "REPLAN_REQUEST\n" + canonical_json(payload)
