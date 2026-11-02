'Additive JSON transport reliability for frozen external evidence Ornith reasoning.'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness.external_evidence.models import ResearchWorkflowPolicy, canonical_json
from harness.external_evidence.reasoning import OrnithResearchReasoner, ResearchReasoningProtocolError

from .policy import ResearchReasoningPolicy


def normalize_json_object_transport(content: str, *, allow_fence: bool = True) -> tuple[str, bool]:
    """Normalize transport decoration without repairing JSON semantics.

    Accepted forms:
    1. exactly one JSON object;
    2. exactly one canonical Markdown `json` fence containing one JSON object.

    Prose before/after the object, generic fences, nested fences, arrays, malformed
    JSON and empty responses remain invalid.
    """
    if not isinstance(content, str):
        raise ResearchReasoningProtocolError("reasoning response content must be string")
    text = content.strip()
    if not text:
        raise ResearchReasoningProtocolError("reasoning response content is empty")

    normalized_fence = False
    candidate = text
    if text.startswith("```") or text.endswith("```"):
        if not allow_fence:
            raise ResearchReasoningProtocolError("JSON markdown fence is not allowed")
        lines = text.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip().casefold() != "```json"
            or lines[-1].strip() != "```"
        ):
            raise ResearchReasoningProtocolError("non-canonical JSON markdown fence")
        candidate = "\n".join(lines[1:-1]).strip()
        if not candidate or "```" in candidate:
            raise ResearchReasoningProtocolError("nested or empty JSON markdown fence")
        normalized_fence = True

    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ResearchReasoningProtocolError(f"reasoning response invalid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ResearchReasoningProtocolError("reasoning response must be JSON object")

    # Canonicalize only after strict JSON parsing. This is transport normalization,
    # not semantic repair; the frozen Phase 18 parser still validates the contract.
    return canonical_json(raw), normalized_fence


@dataclass(frozen=True, slots=True)
class ResearchReasoningMetricsV1:
    model_calls: int
    plan_model_calls: int
    synthesis_model_calls: int
    protocol_failures: int
    repair_calls: int
    fence_normalizations: int

    def to_dict(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "plan_model_calls": self.plan_model_calls,
            "synthesis_model_calls": self.synthesis_model_calls,
            "protocol_failures": self.protocol_failures,
            "repair_calls": self.repair_calls,
            "fence_normalizations": self.fence_normalizations,
        }


@dataclass(frozen=True, slots=True)
class _NormalizedResponse:
    content: str
    model_id: str = ""
    model_tag: str = ""
    context_length: int = 0


class ReliableJsonRuntime:
    """Wrap the frozen Ornith runtime with bounded JSON transport reliability."""

    def __init__(self, inner: Any, policy: ResearchReasoningPolicy) -> None:
        self.inner = inner
        self.policy = policy
        self._model_calls = 0
        self._plan_model_calls = 0
        self._synthesis_model_calls = 0
        self._protocol_failures = 0
        self._repair_calls = 0
        self._fence_normalizations = 0

    @staticmethod
    def _stage(user_prompt: str) -> str:
        if user_prompt.startswith("RESEARCH_PLAN_REQUEST"):
            return "plan"
        if user_prompt.startswith("RESEARCH_SYNTHESIS_REQUEST"):
            return "synthesis"
        return "other"

    def _record_call(self, stage: str) -> None:
        self._model_calls += 1
        if stage == "plan":
            self._plan_model_calls += 1
        elif stage == "synthesis":
            self._synthesis_model_calls += 1

    def _repair_prompt(self, original: str, *, error: str, response: str) -> str:
        previous = response[: self.policy.max_previous_response_chars]
        return (
            "PROTOCOL_RETRY\n"
            "The previous model response violated the JSON transport contract.\n"
            f"previous_error: {error}\n"
            "previous_response_begin\n"
            f"{previous}\n"
            "previous_response_end\n"
            "instruction: Return exactly one JSON object. Do not use markdown fences, "
            "prose, comments, XML, or code blocks. Preserve the requested semantic contract; "
            "do not invent evidence IDs, URLs, topics, or query channels.\n"
            "ORIGINAL_REQUEST\n"
            f"{original}"
        )

    def complete(self, system_prompt: str, user_prompt: str, response_format: str):
        if response_format != "json":
            return self.inner.complete(system_prompt, user_prompt, response_format)

        original_prompt = user_prompt
        current_prompt = user_prompt
        last_error = ""
        last_response = ""
        stage = self._stage(original_prompt)

        for attempt in range(1, self.policy.max_model_attempts + 1):
            response = self.inner.complete(system_prompt, current_prompt, response_format)
            self._record_call(stage)
            content = getattr(response, "content", "")
            try:
                normalized, fenced = normalize_json_object_transport(
                    content,
                    allow_fence=self.policy.allow_canonical_json_markdown_fence,
                )
                if fenced:
                    self._fence_normalizations += 1
                return _NormalizedResponse(
                    content=normalized,
                    model_id=str(getattr(response, "model_id", "")),
                    model_tag=str(getattr(response, "model_tag", "")),
                    context_length=int(getattr(response, "context_length", 0) or 0),
                )
            except ResearchReasoningProtocolError as exc:
                self._protocol_failures += 1
                last_error = str(exc)
                last_response = content if isinstance(content, str) else repr(content)
                if attempt >= self.policy.max_model_attempts:
                    break
                self._repair_calls += 1
                current_prompt = self._repair_prompt(
                    original_prompt,
                    error=last_error,
                    response=last_response,
                )

        raise ResearchReasoningProtocolError(
            f"Phase 18R reasoning exhausted {self.policy.max_model_attempts} attempts: {last_error}"
        )

    def metrics(self) -> ResearchReasoningMetricsV1:
        return ResearchReasoningMetricsV1(
            self._model_calls,
            self._plan_model_calls,
            self._synthesis_model_calls,
            self._protocol_failures,
            self._repair_calls,
            self._fence_normalizations,
        )


class ResilientOrnithResearchReasoner(OrnithResearchReasoner):
    'Frozen external evidence reasoner with an additive reliable JSON runtime boundary.'

    def __init__(
        self,
        root,
        policy: ResearchWorkflowPolicy,
        *,
        reliability_policy: ResearchReasoningPolicy | None = None,
        model_id: str = "ornith_lead",
    ) -> None:
        super().__init__(root, policy, model_id=model_id)
        self.reliability_policy = reliability_policy or ResearchReasoningPolicy.from_project_root(root)
        self.runtime = ReliableJsonRuntime(self.runtime, self.reliability_policy)

    def reliability_metrics(self) -> ResearchReasoningMetricsV1:
        return self.runtime.metrics()
