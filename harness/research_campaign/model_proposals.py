'Strict, proposal-only local-model adapter for research stopping.\n\nThe adapter deliberately returns existing ``ResearchProposalV1`` records.  It\ndoes not select, persist, execute, or expose repository-writing tools.\n'
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..config import ConfigLoader
from ..lead.runtime import OrnithRuntime
from ..models import ModelRegistry
from ..resource_manager.locks import ResourceLocks
from .evidence import ResearchEvidencePacketV1
from .serialization import canonical_json, plain, stable_sha256
from .strategy_contracts import ResearchProposalTargetKindV1, ResearchProposalV1
from .strategy_proposals import (
    DeterministicResearchProposalGeneratorV1,
    ResearchProposalGenerationContextV1,
)


class ModelResearchProposalError(RuntimeError):
    """A generation failure which is diagnostic, never an executable proposal."""


class ModelResearchProposalGenerationPort(Protocol):
    def generate_json(self, *, system_prompt: str, request: Mapping[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class OrnithModelResearchProposalGeneratorPortV1:
    'Configured local CPU Ornith adapter, matching the external evidence runtime path.'

    root: Path
    model_id: str = "ornith_lead"
    runtime_factory: Callable[..., OrnithRuntime] = OrnithRuntime

    @staticmethod
    def _response_schema(request: Mapping[str, Any]) -> dict[str, Any]:
        limit = request.get("max_proposals")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ModelResearchProposalError("structured output requires max_proposals in [1,64]")

        raw_dimensions = request.get("strategy_dimensions")
        if not isinstance(raw_dimensions, (list, tuple)) or not raw_dimensions:
            raise ModelResearchProposalError("structured output requires strategy_dimensions")

        dimension_names: list[str] = []
        target_keys: list[str] = []
        target_values: list[Any] = []
        target_value_keys: set[str] = set()
        for item in raw_dimensions:
            if not isinstance(item, Mapping):
                raise ModelResearchProposalError("strategy_dimensions must contain objects")
            name = item.get("name")
            target = item.get("target")
            key = item.get("key")
            choices = item.get("choices")
            if not isinstance(name, str) or not name:
                raise ModelResearchProposalError("strategy dimension name must be non-empty")
            if not isinstance(target, str) or not target:
                raise ModelResearchProposalError("strategy dimension target must be non-empty")
            resolved_key = key if isinstance(key, str) and key else target.casefold()
            if not isinstance(choices, (list, tuple)) or not choices:
                raise ModelResearchProposalError("strategy dimension choices must be non-empty")
            if name not in dimension_names:
                dimension_names.append(name)
            if resolved_key not in target_keys:
                target_keys.append(resolved_key)
            for value in choices:
                normalized = plain(value)
                fingerprint = canonical_json(normalized)
                if fingerprint not in target_value_keys:
                    target_value_keys.add(fingerprint)
                    target_values.append(normalized)

        raw_evidence = request.get("evidence", ())
        if not isinstance(raw_evidence, (list, tuple)):
            raise ModelResearchProposalError("structured output evidence must be a sequence")
        evidence_ids: list[str] = []
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                raise ModelResearchProposalError("structured output evidence must contain objects")
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ModelResearchProposalError("structured output evidence_id must be non-empty")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)

        evidence_ref_items: dict[str, Any] = {"type": "string"}
        if evidence_ids:
            evidence_ref_items["enum"] = evidence_ids

        required = [
            "target_dimension", "target_key", "target_value", "hypothesis_statement",
            "mechanism", "predicted_effect", "falsifier", "estimated_cost",
            "estimated_risk", "evidence_refs", "confidence",
        ]
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["proposals"],
            "properties": {
                "proposals": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": limit,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": required,
                        "properties": {
                            "target_dimension": {"type": "string", "enum": dimension_names},
                            "target_key": {"type": "string", "enum": target_keys},
                            "target_value": {"enum": target_values},
                            "hypothesis_statement": {"type": "string", "minLength": 1},
                            "mechanism": {"type": "string", "minLength": 1},
                            "predicted_effect": {"type": "string", "minLength": 1},
                            "falsifier": {"type": "string", "minLength": 1},
                            "estimated_cost": {"type": "number", "minimum": 0},
                            "estimated_risk": {"type": "number", "minimum": 0},
                            "evidence_refs": {
                                "type": "array", "minItems": 1, "uniqueItems": True,
                                "items": evidence_ref_items,
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                    },
                }
            },
        }

    def generate_json(self, *, system_prompt: str, request: Mapping[str, Any]) -> str:
        response_schema = self._response_schema(request)
        registry = ModelRegistry.from_loader(ConfigLoader(Path(self.root)))
        runtime = self.runtime_factory(
            registry, ResourceLocks(), timeout_seconds=180, output_tokens=4096,
            model_id=self.model_id,
        )
        try:
            return runtime.complete(system_prompt, canonical_json(request), response_schema).content
        except Exception as exc:  # runtime errors remain diagnostic to the caller
            raise ModelResearchProposalError(f"local model proposal call failed: {type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ModelProposalGenerationOutcomeV1:
    proposals: tuple[ResearchProposalV1, ...]
    model_called: bool
    fallback_used: bool
    diagnostic: str | None = None
    proposal_confidences: tuple[float, ...] = ()
    normalized_response_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchProposalCohortRecordV1:
    cohort_id: str
    campaign_id: str
    cycle_fingerprint: str
    evidence_packet_fingerprint: str
    generator_identity: str
    model_called: bool
    fallback_used: bool
    diagnostic: str | None
    normalized_response_hash: str | None
    proposals: tuple[ResearchProposalV1, ...]
    proposal_confidences: tuple[float, ...]
    created_at: str

    def __post_init__(self) -> None:
        if not self.cohort_id or not self.campaign_id or not self.generator_identity:
            raise ValueError("cohort identity fields must be non-empty")
        if any(item.campaign_id != self.campaign_id for item in self.proposals):
            raise ValueError("cohort proposal campaign mismatch")
        if self.proposal_confidences and len(self.proposal_confidences) != len(self.proposals):
            raise ValueError("proposal_confidences must align with proposals")
        if any(value < 0.0 or value > 1.0 for value in self.proposal_confidences):
            raise ValueError("proposal confidence metadata must be in [0,1]")
        if self.fallback_used and self.normalized_response_hash is not None:
            raise ValueError("fallback outcome cannot masquerade as normalized model output")
        if not self.fallback_used and self.model_called and len(self.proposal_confidences) != len(self.proposals):
            raise ValueError("successful model proposals require confidence metadata")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "cohort_id": self.cohort_id, "campaign_id": self.campaign_id,
                "cycle_fingerprint": self.cycle_fingerprint, "evidence_packet_fingerprint": self.evidence_packet_fingerprint,
                "generator_identity": self.generator_identity, "model_called": self.model_called,
                "fallback_used": self.fallback_used, "diagnostic": self.diagnostic,
                "normalized_response_hash": self.normalized_response_hash,
                "proposal_ids": [item.proposal_id for item in self.proposals],
                "proposals": [item.to_dict() for item in self.proposals],
                "proposal_confidences": list(self.proposal_confidences), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchProposalCohortRecordV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchProposalCohortRecordV1 schema_version must be 1")
        required = {"schema_version", "cohort_id", "campaign_id", "cycle_fingerprint",
                    "evidence_packet_fingerprint", "generator_identity", "model_called", "fallback_used",
                    "diagnostic", "normalized_response_hash", "proposal_ids", "proposals",
                    "proposal_confidences", "created_at"}
        if set(raw) != required:
            raise ValueError("ResearchProposalCohortRecordV1 has missing or unknown fields")
        proposals = tuple(ResearchProposalV1.from_dict(item) for item in raw["proposals"])
        if tuple(raw["proposal_ids"]) != tuple(item.proposal_id for item in proposals):
            raise ValueError("ResearchProposalCohortRecordV1 proposal_ids mismatch")
        return cls(str(raw["cohort_id"]), str(raw["campaign_id"]), str(raw["cycle_fingerprint"]),
                   str(raw["evidence_packet_fingerprint"]), str(raw["generator_identity"]), bool(raw["model_called"]),
                   bool(raw["fallback_used"]), None if raw.get("diagnostic") is None else str(raw["diagnostic"]),
                   None if raw.get("normalized_response_hash") is None else str(raw["normalized_response_hash"]),
                   proposals,
                   tuple(float(item) for item in raw.get("proposal_confidences", ())), str(raw["created_at"]))



_SYSTEM = """You generate bounded research proposals only. You have no tools and no authority to edit files,
run commands, select a winner, change protected evaluation, or request TEST results. Return JSON only with
an object {\"proposals\": [...]}. Each proposal must include target_dimension, target_key, target_value,
hypothesis_statement, mechanism, predicted_effect, falsifier, estimated_cost, estimated_risk,
evidence_refs, and confidence. Target dimension/key must be one of the supplied strategy dimensions."""


@dataclass(frozen=True, slots=True)
class ModelProposalRequestProjectionPolicyV1:
    """Bound only the model-facing projection; durable research authority stays full."""

    max_evidence_items: int = 16
    max_evidence_characters: int = 24_000
    max_novelty_fingerprints: int = 32
    max_rejected_novelty_fingerprints: int = 32
    max_memory_provenance_ids: int = 128

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_evidence_items", 32),
            ("max_evidence_characters", 48_000),
            ("max_novelty_fingerprints", 10_000),
            ("max_rejected_novelty_fingerprints", 10_000),
            ("max_memory_provenance_ids", 2_048),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative integer")
            if value > maximum:
                raise ValueError(f"{name} exceeds authoritative upstream maximum={maximum}")


@dataclass(frozen=True, slots=True)
class ModelBackedResearchProposalGeneratorV1:
    """Bounded model cohort generator with explicit deterministic fallback.

    The output still crosses existing R8 hard admissibility, novelty, utility and
    exactly-one selection.  The fallback is never disguised as a model result.
    """

    model: ModelResearchProposalGenerationPort
    evidence_packet: ResearchEvidencePacketV1 | None = None
    fallback: DeterministicResearchProposalGeneratorV1 | None = None
    max_proposals: int = 8
    request_projection_policy: ModelProposalRequestProjectionPolicyV1 = (
        ModelProposalRequestProjectionPolicyV1()
    )

    def __post_init__(self) -> None:
        if not 1 <= self.max_proposals <= 64:
            raise ValueError("max_proposals must be in [1,64]")
        if not isinstance(
            self.request_projection_policy,
            ModelProposalRequestProjectionPolicyV1,
        ):
            raise ValueError(
                "request_projection_policy must be ModelProposalRequestProjectionPolicyV1"
            )

    def propose(self, context: ResearchProposalGenerationContextV1) -> tuple[ResearchProposalV1, ...]:
        return self.generate(context).proposals

    def generate(self, context: ResearchProposalGenerationContextV1) -> ModelProposalGenerationOutcomeV1:
        if self.evidence_packet is not None and self.evidence_packet.campaign_id != context.champion.campaign_id:
            raise ModelResearchProposalError("evidence packet campaign does not match proposal context")
        limit = min(self.max_proposals, context.budget_snapshot.remaining_experiments, max(0, self.max_proposals - context.generated_proposal_count))
        if limit <= 0:
            return ModelProposalGenerationOutcomeV1((), False, False)
        request = self._request(context, limit)
        try:
            raw = self.model.generate_json(system_prompt=_SYSTEM, request=request)
            proposals, confidences = self._parse(context, raw, limit)
            return ModelProposalGenerationOutcomeV1(proposals, True, False, None, confidences, stable_sha256(json.loads(raw)))
        except Exception as exc:
            if self.fallback is None:
                raise ModelResearchProposalError(f"model proposal cohort rejected: {type(exc).__name__}: {exc}") from exc
            fallback = self.fallback.propose(context)
            return ModelProposalGenerationOutcomeV1(fallback, True, True, f"{type(exc).__name__}: {exc}", (), None)

    def _request(self, context: ResearchProposalGenerationContextV1, limit: int) -> dict[str, Any]:
        policy = self.request_projection_policy
        evidence = self._project_evidence_for_model()

        known = tuple(context.known_novelty_fingerprints)
        rejected = tuple(context.rejected_novelty_fingerprints)
        memory = tuple(context.research_memory_provenance_ids)

        return {
            "schema_version": 1, "max_proposals": limit,
            "campaign_id": context.champion.campaign_id,
            "champion": context.champion.to_dict(), "budget": context.budget_snapshot.to_dict(),
            # These visible samples are informational only. Exact duplicate/rejection
            # authority remains the full typed context used by _parse() and R8.
            "known_novelty_fingerprints": list(
                known[: policy.max_novelty_fingerprints]
            ),
            "known_novelty_fingerprint_count": len(known),
            "known_novelty_fingerprint_digest": stable_sha256(sorted(known)),
            "rejected_novelty_fingerprints": list(
                rejected[: policy.max_rejected_novelty_fingerprints]
            ),
            "rejected_novelty_fingerprint_count": len(rejected),
            "rejected_novelty_fingerprint_digest": stable_sha256(sorted(rejected)),
            "research_memory_provenance_ids": list(
                memory[: policy.max_memory_provenance_ids]
            ),
            "research_memory_provenance_count": len(memory),
            "research_memory_provenance_digest": stable_sha256(list(memory)),
            "strategy_dimensions": [{"name": item.name, "target": item.target.value,
                "key": item.key, "choices": [plain(choice.value) for choice in item.choices]}
                for item in context.strategy_space.dimensions],
            "evidence": evidence,
            "evidence_projection": {
                "source_packet_fingerprint": (
                    None if self.evidence_packet is None else self.evidence_packet.fingerprint
                ),
                "visible_item_count": len(evidence),
                "visible_content_characters": sum(
                    len(str(item["content"])) for item in evidence
                ),
                "source_item_count": (
                    0 if self.evidence_packet is None else len(self.evidence_packet.items)
                ),
                "source_content_characters": (
                    0
                    if self.evidence_packet is None
                    else sum(len(item.content) for item in self.evidence_packet.items)
                ),
            },
            "forbidden": ["repository edits", "shell commands", "worker missions", "winner selection", "protected evaluation", "TEST feedback"],
        }

    def _project_evidence_for_model(self) -> tuple[dict[str, Any], ...]:
        packet = self.evidence_packet
        if packet is None:
            return ()

        policy = self.request_projection_policy
        remaining = policy.max_evidence_characters
        projected: list[dict[str, Any]] = []
        for item in packet.items[: policy.max_evidence_items]:
            if remaining <= 0:
                break
            content = item.content[:remaining]
            if not content:
                continue
            projected.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_type": item.source_type.value,
                    "content": content,
                    "classification": item.classification.value,
                    "confidence": item.confidence,
                }
            )
            remaining -= len(content)
        return tuple(projected)

    def _parse(self, context: ResearchProposalGenerationContextV1, raw: str, limit: int) -> tuple[tuple[ResearchProposalV1, ...], tuple[float, ...]]:
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ModelResearchProposalError("model output is not valid JSON") from exc
        if not isinstance(document, Mapping) or set(document) != {"proposals"}:
            raise ModelResearchProposalError("model output must be an object containing only proposals")
        records = document["proposals"]
        if not isinstance(records, list) or not 1 <= len(records) <= limit:
            raise ModelResearchProposalError("model output proposal count violates bound")
        dimensions = {item.name: item for item in context.strategy_space.dimensions}
        evidence_ids = set() if self.evidence_packet is None else set(self.evidence_packet.provenance_ids)
        proposals: list[ResearchProposalV1] = []
        confidences: list[float] = []
        novelty_seen: set[str] = set()
        for ordinal, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ModelResearchProposalError("every model proposal must be an object")
            required = {"target_dimension", "target_key", "target_value", "hypothesis_statement", "mechanism",
                        "predicted_effect", "falsifier", "estimated_cost", "estimated_risk", "evidence_refs", "confidence"}
            if set(record) != required:
                raise ModelResearchProposalError("model proposal has missing or unknown fields")
            dimension = dimensions.get(record["target_dimension"])
            if dimension is None or str(record["target_key"]) != (dimension.key or dimension.target.value.casefold()):
                raise ModelResearchProposalError("model proposal target is outside the bounded strategy space")
            target_value = self._resolve_target_value(dimension, record["target_value"])
            refs = record["evidence_refs"]
            if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item in evidence_ids for item in refs):
                raise ModelResearchProposalError("model proposal must cite supplied evidence packet IDs")
            confidence = record["confidence"]
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ModelResearchProposalError("model proposal confidence must be in [0,1]")
            normalized_record = dict(plain(record))
            normalized_record["target_value"] = plain(target_value)
            identity = stable_sha256({"campaign": context.champion.campaign_id, "context": context.context_fingerprint,
                                      "ordinal": ordinal, "record": normalized_record})
            proposal = ResearchProposalV1(
                proposal_id=f"p23m-{identity[:24]}", campaign_id=context.champion.campaign_id,
                source_strategy_id=context.champion.strategy_id, parent_experiment_ids=(context.champion.experiment_id,),
                hypothesis_statement=str(record["hypothesis_statement"]), mechanism=str(record["mechanism"]),
                falsifier=str(record["falsifier"]), target_kind=self._target_kind(dimension.target.value),
                target_dimension=dimension.name, target_key=str(record["target_key"]), target_value=target_value,
                predicted_effect=str(record["predicted_effect"]), estimated_cost=float(record["estimated_cost"]),
                estimated_risk=float(record["estimated_risk"]),
                provenance_ids=tuple([f"evidence:{item}" for item in refs] + list(context.research_memory_provenance_ids)),
            )
            current_known, current_value = self._current_target_value(
                context.champion,
                dimension,
            )
            if (
                current_known
                and canonical_json(plain(current_value))
                == canonical_json(plain(target_value))
            ):
                continue
            if proposal.novelty_fingerprint in novelty_seen or proposal.novelty_fingerprint in context.excluded_novelty_fingerprints:
                raise ModelResearchProposalError("model cohort contains duplicate/rejected proposal")
            novelty_seen.add(proposal.novelty_fingerprint)
            proposals.append(proposal)
            confidences.append(float(confidence))
        return tuple(proposals), tuple(confidences)

    @staticmethod
    def _current_target_value(champion: Any, dimension: Any) -> tuple[bool, Any]:
        """Return whether this strategy target already has a champion value."""
        target = dimension.target.value
        key = dimension.key
        if target == "PARAMETER":
            return (False, None) if key not in champion.parameters else (True, champion.parameters[key])
        if target == "MODEL_ID":
            return True, champion.model_id
        if target == "PROVIDER":
            return True, champion.provider
        if target == "MODEL_VERSION":
            return True, champion.model_version
        if target == "PROMPT_HASH":
            return True, champion.prompt_hash
        if target == "SAMPLING_CONFIG":
            return (False, None) if key not in champion.sampling_config else (True, champion.sampling_config[key])
        if target == "TOOL_POLICY_HASH":
            return True, champion.tool_policy_hash
        if target == "SEED":
            return True, champion.seed
        if target == "RESOURCE_BUDGET":
            return (False, None) if key not in champion.resource_budget else (True, champion.resource_budget[key])
        if target == "EXECUTION":
            return (False, None) if key not in champion.execution else (True, champion.execution[key])
        if target == "STRATEGY_ID":
            return True, champion.strategy_id
        raise ModelResearchProposalError(
            f"unsupported strategy target for no-op comparison: {target}"
        )

    @staticmethod
    def _resolve_target_value(dimension: Any, raw_value: Any) -> Any:
        """Return the exact configured strategy choice represented by model JSON.

        Canonical identity remains authoritative. The only compatibility bridge
        is a unique numeric-equivalent match such as JSON ``1`` for configured
        ``1.0``. Booleans are never numeric-compatible and ambiguous numeric
        matches fail closed.
        """

        normalized = plain(raw_value)
        normalized_identity = canonical_json(normalized)
        exact_matches = [
            plain(choice.value)
            for choice in dimension.choices
            if canonical_json(plain(choice.value)) == normalized_identity
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            raise ModelResearchProposalError(
                "model proposal target_value matches multiple canonical strategy choices"
            )

        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
            numeric_matches = []
            for choice in dimension.choices:
                candidate = plain(choice.value)
                if (
                    isinstance(candidate, (int, float))
                    and not isinstance(candidate, bool)
                    and candidate == normalized
                ):
                    numeric_matches.append(candidate)
            if len(numeric_matches) == 1:
                return numeric_matches[0]
            if len(numeric_matches) > 1:
                raise ModelResearchProposalError(
                    "model proposal target_value is ambiguous across numeric strategy choices"
                )

        raise ModelResearchProposalError("model proposal target_value is outside allowed choices")

    @staticmethod
    def _target_kind(target: str) -> ResearchProposalTargetKindV1:
        if target in {"MODEL_ID", "PROVIDER", "MODEL_VERSION"}:
            return ResearchProposalTargetKindV1.MODEL
        if target == "PROMPT_HASH":
            return ResearchProposalTargetKindV1.PROMPT
        if target == "TOOL_POLICY_HASH":
            return ResearchProposalTargetKindV1.TOOL_POLICY
        return ResearchProposalTargetKindV1.EXECUTION
