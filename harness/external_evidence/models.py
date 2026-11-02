'Immutable contracts for external evidence external research and evidence.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds max_length={max_length}")
    return value


def _safe_id(value: object, name: str) -> str:
    value = _text(value, name)
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must match {_ID_RE.pattern}")
    return value


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be positive integer")
    return value


def _non_negative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _ratio(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be in [0,1]")
    return value


def _freeze(value: Any, name: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} contains non-finite float")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            frozen[key] = _freeze(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def stable_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ResearchChannel(StrEnum):
    WEB = "WEB"
    GITHUB_REPOSITORIES = "GITHUB_REPOSITORIES"
    GITHUB_CODE = "GITHUB_CODE"
    GITHUB_ISSUES = "GITHUB_ISSUES"


class SourceKind(StrEnum):
    WEB_PAGE = "WEB_PAGE"
    OFFICIAL_DOC = "OFFICIAL_DOC"
    RESEARCH_PAPER = "RESEARCH_PAPER"
    GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
    GITHUB_CODE = "GITHUB_CODE"
    GITHUB_ISSUE = "GITHUB_ISSUE"
    GITHUB_PULL_REQUEST = "GITHUB_PULL_REQUEST"
    UNKNOWN = "UNKNOWN"


class SourceTier(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    COMMUNITY = "COMMUNITY"
    UNKNOWN = "UNKNOWN"


class ClaimImportance(StrEnum):
    NORMAL = "NORMAL"
    IMPORTANT = "IMPORTANT"
    CRITICAL = "CRITICAL"


class EvidenceRelation(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


class ResearchOutcome(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ResearchWorkflowPolicy:
    max_search_rounds: int
    max_queries_per_round: int
    max_results_per_query: int
    max_sources_total: int
    max_source_bytes: int
    max_source_text_chars: int
    max_evidence_items: int
    max_excerpt_chars: int
    max_claims: int
    min_sources_per_important_claim: int
    min_source_domains: int
    require_primary_for_critical_claims: bool
    require_zero_unresolved_critical_contradictions: bool
    require_read_only_tools: bool
    block_private_network_targets: bool
    max_redirects: int
    request_timeout_seconds: int
    live_research_cases: int
    live_min_success_rate: float
    searxng_enabled: bool
    searxng_base_url_env: str
    searxng_allow_private_backend: bool
    github_enabled: bool
    github_token_env: str
    github_api_base_url: str

    def __post_init__(self) -> None:
        for name in (
            "max_search_rounds", "max_queries_per_round", "max_results_per_query",
            "max_sources_total", "max_source_bytes", "max_source_text_chars",
            "max_evidence_items", "max_excerpt_chars", "max_claims",
            "min_sources_per_important_claim", "min_source_domains", "max_redirects",
            "request_timeout_seconds", "live_research_cases",
        ):
            _positive(getattr(self, name), name)
        _ratio(self.live_min_success_rate, "live_min_success_rate")
        for name in (
            "require_primary_for_critical_claims",
            "require_zero_unresolved_critical_contradictions",
            "require_read_only_tools",
            "block_private_network_targets",
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"Phase 18 invariant requires {name}=true")
        _text(self.searxng_base_url_env, "searxng_base_url_env")
        _text(self.github_token_env, "github_token_env")
        _text(self.github_api_base_url, "github_api_base_url")

    @classmethod
    def from_project_root(cls, root: object) -> "ResearchWorkflowPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase18.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase18.yaml requires schema_version: 1")
        body = raw.get("external_research_v1")
        if not isinstance(body, Mapping):
            raise ValueError("phase18 external_research_v1 must be mapping")
        live = body.get("live_acceptance")
        searx = body.get("searxng")
        github = body.get("github")
        if not all(isinstance(item, Mapping) for item in (live, searx, github)):
            raise ValueError("phase18 live/searxng/github config must be mappings")
        return cls(
            int(body["max_search_rounds"]), int(body["max_queries_per_round"]),
            int(body["max_results_per_query"]), int(body["max_sources_total"]),
            int(body["max_source_bytes"]), int(body["max_source_text_chars"]),
            int(body["max_evidence_items"]), int(body["max_excerpt_chars"]),
            int(body["max_claims"]), int(body["min_sources_per_important_claim"]),
            int(body["min_source_domains"]), body["require_primary_for_critical_claims"],
            body["require_zero_unresolved_critical_contradictions"], body["require_read_only_tools"],
            body["block_private_network_targets"], int(body["max_redirects"]),
            int(body["request_timeout_seconds"]), int(live["research_cases"]),
            float(live["min_success_rate"]), bool(searx["enabled"]),
            str(searx["base_url_env"]), bool(searx["allow_private_backend"]),
            bool(github["enabled"]), str(github["token_env"]), str(github["api_base_url"]),
        )


@dataclass(frozen=True, slots=True)
class ResearchQueryV1:
    query_id: str
    text: str
    channel: ResearchChannel = ResearchChannel.WEB

    def __post_init__(self) -> None:
        _safe_id(self.query_id, "query_id")
        _text(self.text, "query text", max_length=4000)
        if not isinstance(self.channel, ResearchChannel):
            object.__setattr__(self, "channel", ResearchChannel(str(self.channel)))

    def to_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "text": self.text, "channel": self.channel.value}


@dataclass(frozen=True, slots=True)
class ResearchRequestV1:
    research_id: str
    question: str
    required_topics: tuple[str, ...]
    initial_queries: tuple[ResearchQueryV1, ...]
    root_task_sha256: str = ""

    def __post_init__(self) -> None:
        _safe_id(self.research_id, "research_id")
        _text(self.question, "question", max_length=100000)
        topics = tuple(_text(item, "required_topic", max_length=1000) for item in self.required_topics)
        if not topics or len(set(topics)) != len(topics):
            raise ValueError("required_topics must be non-empty and unique")
        object.__setattr__(self, "required_topics", topics)
        if not isinstance(self.initial_queries, tuple):
            object.__setattr__(self, "initial_queries", tuple(self.initial_queries))
        if not self.initial_queries:
            raise ValueError("initial_queries must not be empty")
        if len({q.query_id for q in self.initial_queries}) != len(self.initial_queries):
            raise ValueError("query_id values must be unique")
        if self.root_task_sha256:
            if len(self.root_task_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.root_task_sha256.lower()):
                raise ValueError("root_task_sha256 must be 64 hex characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "research_id": self.research_id,
            "question": self.question,
            "required_topics": list(self.required_topics),
            "initial_queries": [item.to_dict() for item in self.initial_queries],
            "root_task_sha256": self.root_task_sha256,
        }


@dataclass(frozen=True, slots=True)
class SearchHitV1:
    provider_id: str
    query_id: str
    rank: int
    title: str
    url: str
    snippet: str
    kind: SourceKind
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider_id")
        _safe_id(self.query_id, "query_id")
        _positive(self.rank, "rank")
        _text(self.title, "title", max_length=10000)
        _text(self.url, "url", max_length=20000)
        if not isinstance(self.snippet, str):
            raise ValueError("snippet must be string")
        if not isinstance(self.kind, SourceKind):
            object.__setattr__(self, "kind", SourceKind(str(self.kind)))
        object.__setattr__(self, "metadata", _freeze(self.metadata, "metadata"))

    @property
    def hit_id(self) -> str:
        return "hit:" + hashlib.sha256(f"{self.provider_id}\0{self.url}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class FetchedSourceV1:
    provider_id: str
    canonical_url: str
    title: str
    kind: SourceKind
    tier: SourceTier
    content_type: str
    text: str
    bytes_read: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider_id")
        _text(self.canonical_url, "canonical_url", max_length=20000)
        _text(self.title, "title", max_length=10000)
        if not isinstance(self.kind, SourceKind):
            object.__setattr__(self, "kind", SourceKind(str(self.kind)))
        if not isinstance(self.tier, SourceTier):
            object.__setattr__(self, "tier", SourceTier(str(self.tier)))
        _text(self.content_type, "content_type", max_length=300)
        if not isinstance(self.text, str):
            raise ValueError("text must be string")
        _non_negative(self.bytes_read, "bytes_read")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "metadata"))

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def source_id(self) -> str:
        return "src:" + hashlib.sha256(self.canonical_url.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "kind": self.kind.value,
            "tier": self.tier.value,
            "content_type": self.content_type,
            "text": self.text,
            "bytes_read": self.bytes_read,
            "content_sha256": self.content_sha256,
            "metadata": _plain(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EvidenceItemV1:
    evidence_id: str
    source_id: str
    locator: str
    excerpt: str
    topic: str

    def __post_init__(self) -> None:
        _safe_id(self.evidence_id, "evidence_id")
        _safe_id(self.source_id, "source_id")
        _text(self.locator, "locator", max_length=1000)
        _text(self.excerpt, "excerpt", max_length=20000)
        _text(self.topic, "topic", max_length=1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "topic": self.topic,
        }


@dataclass(frozen=True, slots=True)
class ClaimEvidenceRefV1:
    evidence_id: str
    relation: EvidenceRelation

    def __post_init__(self) -> None:
        _safe_id(self.evidence_id, "evidence_id")
        if not isinstance(self.relation, EvidenceRelation):
            object.__setattr__(self, "relation", EvidenceRelation(str(self.relation)))

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "relation": self.relation.value}


@dataclass(frozen=True, slots=True)
class ResearchClaimV1:
    claim_id: str
    text: str
    topic: str
    importance: ClaimImportance
    evidence: tuple[ClaimEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        _safe_id(self.claim_id, "claim_id")
        _text(self.text, "claim text", max_length=12000)
        _text(self.topic, "claim topic", max_length=1000)
        if not isinstance(self.importance, ClaimImportance):
            object.__setattr__(self, "importance", ClaimImportance(str(self.importance)))
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence:
            raise ValueError("claim must cite evidence")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("claim evidence refs must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "topic": self.topic,
            "importance": self.importance.value,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class SynthesisProposalV1:
    summary: str
    claims: tuple[ResearchClaimV1, ...]
    followup_queries: tuple[ResearchQueryV1, ...] = ()

    def __post_init__(self) -> None:
        _text(self.summary, "summary", max_length=100000)
        if not isinstance(self.claims, tuple):
            object.__setattr__(self, "claims", tuple(self.claims))
        if not isinstance(self.followup_queries, tuple):
            object.__setattr__(self, "followup_queries", tuple(self.followup_queries))
        if len({item.claim_id for item in self.claims}) != len(self.claims):
            raise ValueError("claim ids must be unique")
        if len({item.query_id for item in self.followup_queries}) != len(self.followup_queries):
            raise ValueError("followup query ids must be unique")


@dataclass(frozen=True, slots=True)
class ResearchCoverageV1:
    required_topics: int
    covered_topics: int
    source_domains: int
    important_claims: int
    insufficient_important_claims: int
    critical_claims_without_primary: int
    unresolved_critical_contradictions: int
    sufficient: bool

    @property
    def topic_coverage(self) -> float:
        return 1.0 if self.required_topics == 0 else self.covered_topics / self.required_topics

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_topics": self.required_topics,
            "covered_topics": self.covered_topics,
            "topic_coverage": self.topic_coverage,
            "source_domains": self.source_domains,
            "important_claims": self.important_claims,
            "insufficient_important_claims": self.insufficient_important_claims,
            "critical_claims_without_primary": self.critical_claims_without_primary,
            "unresolved_critical_contradictions": self.unresolved_critical_contradictions,
            "sufficient": self.sufficient,
        }


@dataclass(frozen=True, slots=True)
class ResearchReportV1:
    research_id: str
    question: str
    outcome: ResearchOutcome
    summary: str
    sources: tuple[FetchedSourceV1, ...]
    evidence: tuple[EvidenceItemV1, ...]
    claims: tuple[ResearchClaimV1, ...]
    coverage: ResearchCoverageV1
    queries_executed: tuple[ResearchQueryV1, ...]
    search_rounds: int
    failure_reason: str = ""

    def __post_init__(self) -> None:
        _safe_id(self.research_id, "research_id")
        _text(self.question, "question")
        if not isinstance(self.outcome, ResearchOutcome):
            object.__setattr__(self, "outcome", ResearchOutcome(str(self.outcome)))
        if not isinstance(self.summary, str):
            raise ValueError("summary must be string")
        _non_negative(self.search_rounds, "search_rounds")
        for name in ("sources", "evidence", "claims", "queries_executed"):
            if not isinstance(getattr(self, name), tuple):
                object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_text=False))

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        sources = []
        for source in self.sources:
            raw = source.to_dict()
            if not include_text:
                raw.pop("text", None)
            sources.append(raw)
        return {
            "schema_version": 1,
            "research_id": self.research_id,
            "question": self.question,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "sources": sources,
            "evidence": [item.to_dict() for item in self.evidence],
            "claims": [item.to_dict() for item in self.claims],
            "coverage": self.coverage.to_dict(),
            "queries_executed": [item.to_dict() for item in self.queries_executed],
            "search_rounds": self.search_rounds,
            "failure_reason": self.failure_reason,
        }

# Verified legacy import/pickle aliases, not canonical public exports.
Phase18Policy = ResearchWorkflowPolicy
