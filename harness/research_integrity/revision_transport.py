'research integrity-only hardening for research remediation revision structured output.\n\nThis layer is deliberately additive.  Frozen research reasoning remains the final JSON\ntransport authority; research integrity only (1) asks Ollama for a strict revision JSON\nSchema and (2) normalizes a single outer Markdown fence variant when it is\npure transport decoration around exactly one JSON object.\n'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness.external_evidence.models import canonical_json
from harness.research_reasoning.runtime import ReliableJsonRuntime


REVISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "revised_text", "evidence", "reason"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["KEEP", "REVISE", "NARROW", "DROP"],
        },
        "revised_text": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_id", "relation"],
                "properties": {
                    "evidence_id": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": ["SUPPORTS", "CONTRADICTS"],
                    },
                },
            },
        },
        "reason": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    content: str
    model_id: str = ""
    model_tag: str = ""
    context_length: int = 0


@dataclass(frozen=True, slots=True)
class RevisionTransportMetricsV1:
    schema_enforcements: int
    tolerant_fence_normalizations: int

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_enforcements": self.schema_enforcements,
            "tolerant_fence_normalizations": self.tolerant_fence_normalizations,
        }


def _single_outer_fence_json_object(content: str) -> str | None:
    'Return canonical JSON only for one safe outer fence.\n\n    research reasoning already accepts the canonical multiline ```json fence.  This\n    fallback additionally accepts common model-only transport variants such as\n    a generic outer fence or a single-line ```json{...}``` fence.  It rejects\n    prose outside the fence, nested/multiple fences, non-JSON language labels,\n    arrays, malformed JSON, and any non-object payload.\n    '
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not (text.startswith("```") and text.endswith("```")):
        return None
    if text.count("```") != 2:
        return None

    # Preserve Phase18R's existing canonical-fence path and metrics.  This
    # adapter only handles safe variants that frozen Phase18R rejects.
    lines = text.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() == "```json"
        and lines[-1].strip() == "```"
    ):
        return None

    body = text[3:-3].strip()
    if not body:
        return None

    folded = body.casefold()
    if folded.startswith("json"):
        if len(body) > 4 and not (body[4].isspace() or body[4] == "{"):
            return None
        body = body[4:].lstrip()
    elif not body.startswith("{"):
        # Generic fences are allowed only when the payload begins directly
        # with an object.  Other language identifiers stay invalid.
        return None

    if "```" in body:
        return None
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, Mapping):
        return None
    return canonical_json(raw)


class RevisionTransportRuntime:
    """Raw-runtime adapter installed underneath frozen ReliableJsonRuntime."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._schema_enforcements = 0
        self._tolerant_fence_normalizations = 0

    def complete(self, system_prompt: str, user_prompt: str, response_format: Any):
        schema = response_format
        if response_format == "json":
            schema = REVISION_JSON_SCHEMA
            self._schema_enforcements += 1

        response = self.inner.complete(system_prompt, user_prompt, schema)
        content = getattr(response, "content", "")
        normalized = _single_outer_fence_json_object(content)
        if normalized is None:
            return response

        self._tolerant_fence_normalizations += 1
        return _TransportResponse(
            content=normalized,
            model_id=str(getattr(response, "model_id", "")),
            model_tag=str(getattr(response, "model_tag", "")),
            context_length=int(getattr(response, "context_length", 0) or 0),
        )

    def metrics(self) -> RevisionTransportMetricsV1:
        return RevisionTransportMetricsV1(
            self._schema_enforcements,
            self._tolerant_fence_normalizations,
        )


def harden_revision_judge_transport(judge: Any) -> RevisionTransportRuntime:
    'Install research integrity transport beneath the frozen research reasoning wrapper.\n\n    The frozen research remediation judge owns a ReliableJsonRuntime.  We do not replace or\n    bypass it; instead we wrap its raw inner Ornith runtime so research reasoning still\n    parses/canonicalizes and remains fail-closed authority.\n    '
    runtime = getattr(judge, "runtime", None)
    if not isinstance(runtime, ReliableJsonRuntime):
        raise TypeError("Phase20Q requires Phase19Q judge runtime to be ReliableJsonRuntime")
    if isinstance(runtime.inner, RevisionTransportRuntime):
        return runtime.inner
    wrapped = RevisionTransportRuntime(runtime.inner)
    runtime.inner = wrapped
    return wrapped

# Verified compatibility aliases for historical Python interfaces.
Phase20QRevisionTransportRuntime = RevisionTransportRuntime
harden_phase19q_revision_judge = harden_revision_judge_transport
