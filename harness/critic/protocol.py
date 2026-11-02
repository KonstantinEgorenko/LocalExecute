"""Strict JSON Schema and parser for Ministral critic output."""
from __future__ import annotations

import json
from typing import Any

from .errors import CriticProtocolError
from .models import CriticAnalysis, CriticEvidenceBundle

CRITIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "deterministic_outcome", "verdict", "failure_class", "confidence", "summary",
        "evidence", "recommended_action", "retry_guidance", "can_override_deterministic",
    ],
    "properties": {
        "deterministic_outcome": {"type": "string", "enum": ["PASSED", "FAILED"]},
        "verdict": {"type": "string", "enum": ["ACCEPT", "CONCERN", "RETRY", "ESCALATE", "REPLAN", "BLOCKED"]},
        "failure_class": {"type": "string", "enum": [
            "NONE", "IMPLEMENTATION_ERROR", "TEST_FAILURE", "ENVIRONMENT_ERROR", "TOOL_ERROR",
            "CONTRACT_ERROR", "CONTEXT_INSUFFICIENT", "ROUTING_ERROR", "WORKER_ERROR", "UNKNOWN",
        ]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 32, "uniqueItems": True},
        "recommended_action": {"type": "string", "enum": [
            "NONE", "RETRY_SAME_WORKER", "RETRY_WITH_CONTEXT", "ESCALATE_WORKER", "REPLAN", "HUMAN_REVIEW", "STOP",
        ]},
        "retry_guidance": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 8, "uniqueItems": True},
        "can_override_deterministic": {"type": "boolean", "const": False},
    },
}

# The full response contract is enforced locally. Ollama transport uses JSON mode
# for maximum compatibility across local model families.
_EXPECTED_KEYS = frozenset(CRITIC_RESPONSE_SCHEMA["required"])


def _validate_local_contract(payload: dict[str, Any]) -> None:
    string_fields = ("deterministic_outcome", "verdict", "failure_class", "summary", "recommended_action")
    for key in string_fields:
        if not isinstance(payload.get(key), str):
            raise CriticProtocolError(f"critic response {key} must be a string")
    summary = payload["summary"]
    if not summary or len(summary) > 4000:
        raise CriticProtocolError("critic response summary length must be in [1, 4000]")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise CriticProtocolError("critic response confidence must be numeric")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not (1 <= len(evidence) <= 32):
        raise CriticProtocolError("critic response evidence must contain 1..32 items")
    if any(not isinstance(item, str) or not item for item in evidence):
        raise CriticProtocolError("critic response evidence items must be non-empty strings")
    if len(evidence) != len(set(evidence)):
        raise CriticProtocolError("critic response evidence must contain unique items")
    guidance = payload.get("retry_guidance")
    if not isinstance(guidance, list) or len(guidance) > 8:
        raise CriticProtocolError("critic response retry_guidance must contain at most 8 items")
    if any(not isinstance(item, str) or not item or len(item) > 1000 for item in guidance):
        raise CriticProtocolError("critic response retry_guidance items must be non-empty strings up to 1000 chars")
    if len(guidance) != len(set(guidance)):
        raise CriticProtocolError("critic response retry_guidance must contain unique items")
    if payload.get("can_override_deterministic") is not False:
        raise CriticProtocolError("critic response can_override_deterministic must be false")


def parse_critic_response(content: str, bundle: CriticEvidenceBundle) -> CriticAnalysis:
    if not isinstance(content, str) or not content.strip():
        raise CriticProtocolError("critic response must be non-empty JSON text")
    if content.lstrip().startswith("```"):
        raise CriticProtocolError("critic response must not use markdown fences")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CriticProtocolError(f"critic response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CriticProtocolError("critic response must be a JSON object")
    if set(payload) != _EXPECTED_KEYS:
        raise CriticProtocolError("critic response keys must match schema exactly")
    _validate_local_contract(payload)
    allowed = bundle.allowed_evidence_ids
    alias_map: dict[str, str] = {}
    if "candidate_diff" in allowed:
        alias_map[f"candidate_diff:{bundle.candidate_diff_sha256}"] = "candidate_diff"
        alias_map[f"candidate_diff:{bundle.candidate_diff_sha256[:16]}"] = "candidate_diff"
    canonical_evidence: list[str] = []
    unknown: list[str] = []
    for evidence_id in payload["evidence"]:
        if evidence_id in allowed:
            canonical_evidence.append(evidence_id)
        elif evidence_id in alias_map:
            canonical_evidence.append(alias_map[evidence_id])
        else:
            unknown.append(evidence_id)
    if unknown:
        raise CriticProtocolError(f"critic cited unknown evidence IDs: {sorted(set(unknown))}; allowed evidence IDs: {sorted(allowed)}")
    if len(canonical_evidence) != len(set(canonical_evidence)):
        raise CriticProtocolError("critic evidence references collapse to duplicate canonical IDs")
    normalized_payload = dict(payload)
    normalized_payload["evidence"] = canonical_evidence
    try:
        analysis = CriticAnalysis.from_dict(normalized_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise CriticProtocolError(f"invalid critic response: {exc}") from exc
    if analysis.deterministic_outcome is not bundle.verification_outcome:
        raise CriticProtocolError("critic changed deterministic verification outcome")
    if not analysis.evidence:
        raise CriticProtocolError("critic must cite at least one known evidence ID")
    return analysis
