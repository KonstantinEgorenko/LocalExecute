'Dynamic JSON Schema for runtime hardening evidence-bound synthesis.'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness.external_evidence.models import ClaimImportance, EvidenceRelation, ResearchChannel

_PREFIX = "RESEARCH_SYNTHESIS_REQUEST\n"
_ORIGINAL_MARKER = "ORIGINAL_REQUEST\n" + _PREFIX

# runtime hardening model-facing output projection. These are intentionally much tighter
# than frozen Phase18's broad storage/controller ceiling (max_claims=48).
# Quality/stopping gates remain responsible for declaring insufficiency.
_MAX_CLAIMS_TOTAL = 8
_MAX_CLAIMS_PER_TOPIC = 4
_MAX_FOLLOWUPS_TOTAL = 6
_MAX_FOLLOWUPS_PER_TOPIC = 4
_MAX_EVIDENCE_REFS_PER_CLAIM = 4
_MAX_SUMMARY_CHARS = 600
_MAX_CLAIM_TEXT_CHARS = 650
_MAX_FOLLOWUP_TEXT_CHARS = 400
_MAX_IDENTIFIER_CHARS = 96


class SynthesisSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesisOutputBounds:
    max_claims: int
    max_followups: int
    max_evidence_refs_per_claim: int = _MAX_EVIDENCE_REFS_PER_CLAIM
    max_summary_chars: int = _MAX_SUMMARY_CHARS
    max_claim_text_chars: int = _MAX_CLAIM_TEXT_CHARS
    max_followup_text_chars: int = _MAX_FOLLOWUP_TEXT_CHARS
    max_identifier_chars: int = _MAX_IDENTIFIER_CHARS


def _payload_from_synthesis_prompt(user_prompt: str) -> Mapping[str, Any] | None:
    marker_index = user_prompt.rfind(_ORIGINAL_MARKER)
    if marker_index >= 0:
        body = user_prompt[marker_index + len(_ORIGINAL_MARKER):]
    elif user_prompt.startswith(_PREFIX):
        body = user_prompt[len(_PREFIX):]
    else:
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SynthesisSchemaError(
            f"cannot parse bounded synthesis payload for schema: {exc}"
        ) from exc

    if not isinstance(payload, Mapping):
        raise SynthesisSchemaError("bounded synthesis payload must be object")
    return payload


def _unique_strings(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def synthesis_output_bounds(user_prompt: str) -> SynthesisOutputBounds | None:
    payload = _payload_from_synthesis_prompt(user_prompt)
    if payload is None:
        return None
    topic_count = max(1, len(_unique_strings(payload.get("required_topics", []))))
    return SynthesisOutputBounds(
        max_claims=min(
            _MAX_CLAIMS_TOTAL,
            max(_MAX_CLAIMS_PER_TOPIC, topic_count * 2),
        ),
        max_followups=min(
            _MAX_FOLLOWUPS_TOTAL,
            max(_MAX_FOLLOWUPS_PER_TOPIC, topic_count),
        ),
    )


def synthesis_output_instruction(user_prompt: str) -> str | None:
    bounds = synthesis_output_bounds(user_prompt)
    if bounds is None:
        return None
    return (
        "PHASE20R_OUTPUT_BOUNDS: "
        f"summary<={bounds.max_summary_chars} chars; "
        f"claims<={bounds.max_claims}; "
        f"claim.text<={bounds.max_claim_text_chars} chars; "
        f"evidence refs/claim<={bounds.max_evidence_refs_per_claim}; "
        f"followup_queries<={bounds.max_followups}; "
        f"followup text<={bounds.max_followup_text_chars} chars. "
        "Prefer the strongest non-redundant evidence-bound claims. "
        "When more research is needed, use bounded followup_queries instead of "
        "expanding the response. Do not invent or automatically attach evidence."
    )


def synthesis_json_schema(user_prompt: str) -> dict[str, Any] | None:
    payload = _payload_from_synthesis_prompt(user_prompt)
    if payload is None:
        return None

    bounds = synthesis_output_bounds(user_prompt)
    assert bounds is not None

    topics = _unique_strings(payload.get("required_topics", []))
    evidence_raw = payload.get("evidence", [])
    evidence_ids = _unique_strings(
        item.get("evidence_id", "")
        for item in evidence_raw
        if isinstance(item, Mapping)
    ) if isinstance(evidence_raw, list) else []

    topic_schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if topics:
        topic_schema["enum"] = topics

    claim_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claim_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": bounds.max_identifier_chars,
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": bounds.max_claim_text_chars,
            },
            "topic": topic_schema,
            "importance": {
                "type": "string",
                "enum": [item.value for item in ClaimImportance],
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": bounds.max_evidence_refs_per_claim,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "evidence_id": {
                            "type": "string",
                            "enum": evidence_ids,
                        },
                        "relation": {
                            "type": "string",
                            "enum": [item.value for item in EvidenceRelation],
                        },
                    },
                    "required": ["evidence_id", "relation"],
                },
            },
        },
        "required": ["claim_id", "text", "topic", "importance", "evidence"],
    }

    followup_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": bounds.max_identifier_chars,
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": bounds.max_followup_text_chars,
            },
            "channel": {
                "type": "string",
                "enum": [item.value for item in ResearchChannel],
            },
        },
        "required": ["query_id", "text", "channel"],
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {
                "type": "string",
                "maxLength": bounds.max_summary_chars,
            },
            "claims": {
                "type": "array",
                "maxItems": bounds.max_claims,
                "items": claim_schema,
            },
            "followup_queries": {
                "type": "array",
                "maxItems": bounds.max_followups,
                "items": followup_schema,
            },
        },
        "required": ["summary", "claims", "followup_queries"],
    }
