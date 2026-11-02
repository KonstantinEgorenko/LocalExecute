'Additive semantic-contract reliability after research reasoning transport normalization.'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness.external_evidence.models import (
    ClaimImportance,
    EvidenceRelation,
    ResearchWorkflowPolicy,
    ResearchChannel,
    canonical_json,
)
from harness.external_evidence.reasoning import ResearchReasoningProtocolError
from harness.research_reasoning import ResearchReasoningPolicy, ResilientOrnithResearchReasoner

from .policy import ResearchSemanticPolicy


def _json_object(content: str) -> Mapping[str, Any]:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchReasoningProtocolError(f"semantic boundary received invalid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ResearchReasoningProtocolError("semantic boundary requires JSON object")
    return raw


def _payload(original_prompt: str, prefix: str) -> Mapping[str, Any]:
    if not original_prompt.startswith(prefix + "\n"):
        raise ResearchReasoningProtocolError(f"semantic boundary missing {prefix} prefix")
    body = original_prompt.split("\n", 1)[1]
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResearchReasoningProtocolError(f"original {prefix} payload invalid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ResearchReasoningProtocolError(f"original {prefix} payload must be object")
    return raw


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_query(item: object) -> str | None:
    if not isinstance(item, Mapping):
        return "query must be object"
    if not _nonempty_string(item.get("query_id")):
        return "query_id must be non-empty string"
    if not _nonempty_string(item.get("text")):
        return "query text must be non-empty string"
    try:
        ResearchChannel(str(item.get("channel", "")))
    except ValueError:
        return f"invalid query channel={item.get('channel')!r}"
    return None


def _validate_plan(raw: Mapping[str, Any], original_prompt: str) -> str | None:
    request = _payload(original_prompt, "RESEARCH_PLAN_REQUEST")
    queries = raw.get("queries")
    max_queries = int(request.get("max_queries", 0) or 0)
    if not isinstance(queries, list) or not queries:
        return "top-level queries must be a non-empty list; do not echo request envelope or contract"
    if max_queries > 0 and len(queries) > max_queries:
        return f"top-level queries exceeds max_queries={max_queries}"
    ids = []
    for item in queries:
        error = _validate_query(item)
        if error:
            return error
        ids.append(str(item["query_id"]))
    if len(ids) != len(set(ids)):
        return "query_id values must be unique"
    return None


def _validate_synthesis(raw: Mapping[str, Any], original_prompt: str) -> str | None:
    request = _payload(original_prompt, "RESEARCH_SYNTHESIS_REQUEST")
    summary = raw.get("summary")
    claims = raw.get("claims")
    followups = raw.get("followup_queries", [])
    if not _nonempty_string(summary):
        return "top-level summary must be a non-empty string; do not echo request envelope or contract"
    if not isinstance(claims, list):
        return "top-level claims must be a list"
    if not isinstance(followups, list):
        return "top-level followup_queries must be a list when present"

    allowed_evidence = {
        str(item.get("evidence_id"))
        for item in request.get("evidence", [])
        if isinstance(item, Mapping) and _nonempty_string(item.get("evidence_id"))
    }
    allowed_topics = {
        str(item).casefold()
        for item in request.get("required_topics", [])
        if _nonempty_string(item)
    }
    claim_ids = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            return "claim must be object"
        for key in ("claim_id", "text", "topic", "importance"):
            if not _nonempty_string(claim.get(key)):
                return f"claim {key} must be non-empty string"
        claim_ids.append(str(claim["claim_id"]))
        if str(claim["topic"]).casefold() not in allowed_topics:
            return f"claim topic outside required_topics: {claim['topic']}"
        try:
            ClaimImportance(str(claim["importance"]))
        except ValueError:
            return f"invalid claim importance={claim['importance']!r}"
        refs = claim.get("evidence")
        if not isinstance(refs, list) or not refs:
            return "claim evidence must be non-empty list"
        seen_refs = []
        for ref in refs:
            if not isinstance(ref, Mapping):
                return "claim evidence ref must be object"
            evidence_id = str(ref.get("evidence_id", ""))
            if evidence_id not in allowed_evidence:
                return f"claim cites unknown evidence_id={evidence_id}"
            try:
                EvidenceRelation(str(ref.get("relation", "")))
            except ValueError:
                return f"invalid evidence relation={ref.get('relation')!r}"
            seen_refs.append(evidence_id)
        if len(seen_refs) != len(set(seen_refs)):
            return f"claim evidence refs must be unique for claim_id={claim['claim_id']}"
    if len(claim_ids) != len(set(claim_ids)):
        return "claim_id values must be unique"

    followup_ids = []
    for query in followups:
        error = _validate_query(query)
        if error:
            return f"followup {error}"
        followup_ids.append(str(query["query_id"]))
    if len(followup_ids) != len(set(followup_ids)):
        return "followup query_id values must be unique"
    return None


def validate_stage_contract(content: str, original_prompt: str) -> tuple[str, str | None]:
    """Return stage and semantic error. Never transforms semantic content."""
    raw = _json_object(content)
    if original_prompt.startswith("RESEARCH_PLAN_REQUEST\n"):
        return "plan", _validate_plan(raw, original_prompt)
    if original_prompt.startswith("RESEARCH_SYNTHESIS_REQUEST\n"):
        return "synthesis", _validate_synthesis(raw, original_prompt)
    return "other", None


@dataclass(frozen=True, slots=True)
class ResearchSemanticMetricsV1:
    semantic_attempts: int
    semantic_failures: int
    semantic_repair_calls: int
    plan_semantic_failures: int
    synthesis_semantic_failures: int

    def to_dict(self) -> dict[str, int]:
        return {
            "semantic_attempts": self.semantic_attempts,
            "semantic_failures": self.semantic_failures,
            "semantic_repair_calls": self.semantic_repair_calls,
            "plan_semantic_failures": self.plan_semantic_failures,
            "synthesis_semantic_failures": self.synthesis_semantic_failures,
        }


class SemanticContractRuntime:
    'Validate model semantics before the frozen external evidence parser sees the output.'

    def __init__(self, inner: Any, policy: ResearchSemanticPolicy) -> None:
        self.inner = inner
        self.policy = policy
        self._semantic_attempts = 0
        self._semantic_failures = 0
        self._semantic_repair_calls = 0
        self._plan_semantic_failures = 0
        self._synthesis_semantic_failures = 0

    def _repair_prompt(self, original: str, stage: str, error: str, response: str) -> str:
        """Build a schema-guided retry without repairing model semantics in code."""
        previous = response[: self.policy.max_previous_response_chars]

        if stage == "plan":
            prefix = "RESEARCH_PLAN_REQUEST"
            request = _payload(original, prefix)
            allowed_channels = [
                str(item)
                for item in request.get("allowed_channels", [])
                if _nonempty_string(item)
            ] or [item.value for item in ResearchChannel]
            max_queries = int(request.get("max_queries", 0) or 0)

            guidance = {
                "required_top_level_keys": ["queries"],
                "max_queries": max_queries,
                "allowed_channels": allowed_channels,
                "query_required_fields": ["query_id", "text", "channel"],
                "example_shape": {
                    "queries": [
                        {
                            "query_id": "q1",
                            "text": "specific search query",
                            "channel": allowed_channels[0],
                        }
                    ]
                },
                "rules": [
                    "Return only the top-level contract value; never echo research_id, question, required_topics, max_queries, allowed_channels, notes, or contract.",
                    "Every query must include non-empty query_id, text, and channel.",
                    "Every channel must be one of allowed_channels.",
                    "query_id values must be unique.",
                ],
            }
        else:
            prefix = "RESEARCH_SYNTHESIS_REQUEST"
            request = _payload(original, prefix)
            allowed_topics = [
                str(item)
                for item in request.get("required_topics", [])
                if _nonempty_string(item)
            ]
            allowed_evidence_ids = [
                str(item.get("evidence_id"))
                for item in request.get("evidence", [])
                if isinstance(item, Mapping) and _nonempty_string(item.get("evidence_id"))
            ]
            allowed_channels = [item.value for item in ResearchChannel]
            example_topic = allowed_topics[0] if allowed_topics else "required_topic"
            example_evidence = (
                allowed_evidence_ids[0] if allowed_evidence_ids else "NO_EVIDENCE_AVAILABLE"
            )

            guidance = {
                "required_top_level_keys": ["summary", "claims", "followup_queries"],
                "allowed_topics": allowed_topics,
                "allowed_evidence_ids": allowed_evidence_ids,
                "allowed_importance": [item.value for item in ClaimImportance],
                "allowed_relations": [item.value for item in EvidenceRelation],
                "allowed_followup_channels": allowed_channels,
                "claim_required_fields": [
                    "claim_id",
                    "text",
                    "topic",
                    "importance",
                    "evidence",
                ],
                "evidence_ref_required_fields": ["evidence_id", "relation"],
                "example_shape": {
                    "summary": "evidence-bound synthesis",
                    "claims": [
                        {
                            "claim_id": "claim:1",
                            "text": "one factual claim supported by supplied evidence",
                            "topic": example_topic,
                            "importance": "IMPORTANT",
                            "evidence": [
                                {
                                    "evidence_id": example_evidence,
                                    "relation": "SUPPORTS",
                                }
                            ],
                        }
                    ] if allowed_evidence_ids and allowed_topics else [],
                    "followup_queries": [],
                },
                "rules": [
                    "Return only the top-level contract value; never echo question, required_topics, round_index, sources, evidence, or contract.",
                    "Every emitted claim must contain ALL claim_required_fields with non-empty claim_id, text, topic, and importance.",
                    "claim.topic must be exactly one value from allowed_topics; never omit topic and never use an empty string.",
                    "Every evidence reference must use an evidence_id exactly from allowed_evidence_ids.",
                    "importance must be one of allowed_importance and relation must be one of allowed_relations.",
                    "If supplied evidence cannot support a valid complete claim, omit that claim. Use claims=[] and emit bounded followup_queries instead of emitting a partial claim.",
                    "Never invent evidence IDs, topics, URLs, source facts, query channels, or enum values.",
                ],
            }

        return (
            f"{prefix}\n"
            "SEMANTIC_CONTRACT_RETRY\n"
            f"previous_error: {error}\n"
            "previous_response_begin\n"
            f"{previous}\n"
            "previous_response_end\n"
            "SCHEMA_GUIDANCE\n"
            f"{canonical_json(guidance)}\n"
            "instruction: Correct the previous response to exactly satisfy SCHEMA_GUIDANCE. "
            "Return exactly one JSON object and nothing else. Do not invent data.\n"
            "ORIGINAL_REQUEST\n"
            f"{original}"
        )

    def complete(self, system_prompt: str, user_prompt: str, response_format: str):
        if response_format != "json" or not (
            user_prompt.startswith("RESEARCH_PLAN_REQUEST\n")
            or user_prompt.startswith("RESEARCH_SYNTHESIS_REQUEST\n")
        ):
            return self.inner.complete(system_prompt, user_prompt, response_format)

        original = user_prompt
        current = user_prompt
        last_error = ""
        last_response = ""
        stage = "plan" if original.startswith("RESEARCH_PLAN_REQUEST\n") else "synthesis"

        for attempt in range(1, self.policy.max_semantic_attempts + 1):
            response = self.inner.complete(system_prompt, current, response_format)
            self._semantic_attempts += 1
            content = getattr(response, "content", "")
            _, error = validate_stage_contract(content, original)
            if error is None:
                return response

            self._semantic_failures += 1
            if stage == "plan":
                self._plan_semantic_failures += 1
            else:
                self._synthesis_semantic_failures += 1
            last_error = error
            last_response = content if isinstance(content, str) else repr(content)
            if attempt >= self.policy.max_semantic_attempts:
                break
            self._semantic_repair_calls += 1
            current = self._repair_prompt(original, stage, error, last_response)

        raise ResearchReasoningProtocolError(
            f"Phase 18R2 {stage} semantic contract exhausted "
            f"{self.policy.max_semantic_attempts} attempts: {last_error}"
        )

    def metrics(self):
        'Preserve research reasoning metric API for existing callers.'
        return self.inner.metrics()

    def semantic_metrics(self) -> ResearchSemanticMetricsV1:
        return ResearchSemanticMetricsV1(
            self._semantic_attempts,
            self._semantic_failures,
            self._semantic_repair_calls,
            self._plan_semantic_failures,
            self._synthesis_semantic_failures,
        )


class ContractReliableOrnithResearchReasoner(ResilientOrnithResearchReasoner):
    'external evidence + research reasoning with bounded semantic-contract repair.'

    def __init__(
        self,
        root,
        policy: ResearchWorkflowPolicy,
        *,
        transport_policy: ResearchReasoningPolicy | None = None,
        semantic_policy: ResearchSemanticPolicy | None = None,
        model_id: str = "ornith_lead",
    ) -> None:
        super().__init__(
            root,
            policy,
            reliability_policy=transport_policy or ResearchReasoningPolicy.from_project_root(root),
            model_id=model_id,
        )
        self.semantic_policy = semantic_policy or ResearchSemanticPolicy.from_project_root(root)
        self.runtime = SemanticContractRuntime(self.runtime, self.semantic_policy)

    def semantic_metrics(self) -> ResearchSemanticMetricsV1:
        return self.runtime.semantic_metrics()
