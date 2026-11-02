'Immutable contracts for context management context and artifact assembly.'
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from ..control_plane import WorkContract

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class ArtifactKind(StrEnum):
    SOURCE_FILE = "SOURCE_FILE"
    TEST_FILE = "TEST_FILE"
    CONFIG = "CONFIG"
    WORKER_RESULT = "WORKER_RESULT"
    TEST_RESULT = "TEST_RESULT"
    DIFF = "DIFF"
    LOG = "LOG"
    DOCUMENTATION = "DOCUMENTATION"
    CONTROL_PLANE_EVIDENCE = "CONTROL_PLANE_EVIDENCE"
    OTHER = "OTHER"


class ArtifactOrigin(StrEnum):
    REPOSITORY = "REPOSITORY"
    WORKER = "WORKER"
    CONTROL_PLANE = "CONTROL_PLANE"
    TOOL = "TOOL"
    USER = "USER"


class ContextSource(StrEnum):
    TODO = "TODO"
    WORK_CONTRACT = "WORK_CONTRACT"
    ARTIFACT = "ARTIFACT"
    DEPENDENCY_EVIDENCE = "DEPENDENCY_EVIDENCE"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-boolean integer >= 0")
    return value


def _positive_int(value: object, field_name: str) -> int:
    result = _non_negative_int(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} must be > 0")
    return result


def _id_references(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple of ID references")
    normalized = tuple(_required_text(item, field_name) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _freeze_json(value: Any, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite floats")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-safe")


def _freeze_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, JsonValue]:
    if value is None:
        return MappingProxyType({})
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return frozen


def _plain(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def content_sha256(content: str) -> str:
    """Return the canonical SHA-256 for UTF-8 text content."""
    _text(content, "content")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256(value: object, field_name: str) -> str:
    text = _required_text(value, field_name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 hex digest")
    return text


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    kind: ArtifactKind
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _required_text(self.artifact_id, "artifact_id")
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError("kind must be an ArtifactKind")
        object.__setattr__(self, "sha256", _sha256(self.sha256, "sha256"))
        _non_negative_int(self.size_bytes, "size_bytes")

    @classmethod
    def from_artifact(cls, artifact: "Artifact") -> "ArtifactRef":
        if not isinstance(artifact, Artifact):
            raise ValueError("artifact must be an Artifact")
        return cls(artifact.artifact_id, artifact.kind, artifact.sha256, artifact.size_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    kind: ArtifactKind
    origin: ArtifactOrigin
    content: str
    sha256: str
    size_bytes: int
    media_type: str
    source: str
    created_at: str
    scope_tags: tuple[str, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.artifact_id, "artifact_id")
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError("kind must be an ArtifactKind")
        if not isinstance(self.origin, ArtifactOrigin):
            raise ValueError("origin must be an ArtifactOrigin")
        _text(self.content, "content")
        expected_hash = content_sha256(self.content)
        normalized_hash = _sha256(self.sha256, "sha256")
        if normalized_hash != expected_hash:
            raise ValueError("sha256 must match UTF-8 artifact content")
        object.__setattr__(self, "sha256", normalized_hash)
        expected_size = len(self.content.encode("utf-8"))
        _non_negative_int(self.size_bytes, "size_bytes")
        if self.size_bytes != expected_size:
            raise ValueError("size_bytes must match UTF-8 artifact content")
        for field_name in ("media_type", "source", "created_at"):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(self, "scope_tags", _id_references(self.scope_tags, "scope_tags"))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))

    @classmethod
    def from_text(
        cls,
        artifact_id: str,
        kind: ArtifactKind,
        origin: ArtifactOrigin,
        content: str,
        media_type: str,
        source: str,
        created_at: str,
        scope_tags: tuple[str, ...] = (),
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> "Artifact":
        _text(content, "content")
        return cls(
            artifact_id=artifact_id,
            kind=kind,
            origin=origin,
            content=content,
            sha256=content_sha256(content),
            size_bytes=len(content.encode("utf-8")),
            media_type=media_type,
            source=source,
            created_at=created_at,
            scope_tags=scope_tags,
            metadata={} if metadata is None else metadata,
        )

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef.from_artifact(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "origin": self.origin.value,
            "content": self.content,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "source": self.source,
            "created_at": self.created_at,
            "scope_tags": list(self.scope_tags),
            "metadata": _plain(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_tokens: int
    max_items: int
    max_bytes: int

    def __post_init__(self) -> None:
        _positive_int(self.max_tokens, "max_tokens")
        _positive_int(self.max_items, "max_items")
        _positive_int(self.max_bytes, "max_bytes")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_tokens": self.max_tokens,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class ContextItem:
    item_id: str
    source: ContextSource
    content: str
    required: bool
    priority: int
    token_estimate: int
    provenance: str
    artifact_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _required_text(self.item_id, "item_id")
        if not isinstance(self.source, ContextSource):
            raise ValueError("source must be a ContextSource")
        _text(self.content, "content")
        if not isinstance(self.required, bool):
            raise ValueError("required must be a bool")
        _non_negative_int(self.priority, "priority")
        _non_negative_int(self.token_estimate, "token_estimate")
        _required_text(self.provenance, "provenance")
        if self.artifact_ref is not None and not isinstance(self.artifact_ref, ArtifactRef):
            raise ValueError("artifact_ref must be an ArtifactRef or None")
        if self.source in (ContextSource.ARTIFACT, ContextSource.DEPENDENCY_EVIDENCE) and self.artifact_ref is None:
            raise ValueError(f"{self.source.value} context requires artifact_ref")
        if self.source in (ContextSource.TODO, ContextSource.WORK_CONTRACT) and self.artifact_ref is not None:
            raise ValueError(f"{self.source.value} context must not carry artifact_ref")

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source": self.source.value,
            "content": self.content,
            "required": self.required,
            "priority": self.priority,
            "token_estimate": self.token_estimate,
            "size_bytes": self.size_bytes,
            "provenance": self.provenance,
            "artifact_ref": self.artifact_ref.to_dict() if self.artifact_ref else None,
        }


@dataclass(frozen=True, slots=True)
class ContextRequest:
    request_id: str
    goal_id: str
    todo_id: str
    objective: str
    work_contract: WorkContract
    required_artifacts: tuple[ArtifactRef, ...]
    optional_artifacts: tuple[ArtifactRef, ...]
    budget: ContextBudget

    def __post_init__(self) -> None:
        for field_name in ("request_id", "goal_id", "todo_id", "objective"):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.work_contract, WorkContract):
            raise ValueError("work_contract must be a WorkContract")
        if self.work_contract.goal_id != self.goal_id or self.work_contract.todo_id != self.todo_id:
            raise ValueError("work_contract identity must match ContextRequest goal_id/todo_id")
        required = tuple(self.required_artifacts)
        optional = tuple(self.optional_artifacts)
        if any(not isinstance(item, ArtifactRef) for item in required + optional):
            raise ValueError("required_artifacts and optional_artifacts must contain ArtifactRef values")
        required_ids = [item.artifact_id for item in required]
        optional_ids = [item.artifact_id for item in optional]
        if len(set(required_ids)) != len(required_ids):
            raise ValueError("required_artifacts must not contain duplicate artifact_id values")
        if len(set(optional_ids)) != len(optional_ids):
            raise ValueError("optional_artifacts must not contain duplicate artifact_id values")
        if set(required_ids) & set(optional_ids):
            raise ValueError("required_artifacts and optional_artifacts must not overlap")
        object.__setattr__(self, "required_artifacts", required)
        object.__setattr__(self, "optional_artifacts", optional)
        if not isinstance(self.budget, ContextBudget):
            raise ValueError("budget must be a ContextBudget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "objective": self.objective,
            "work_contract": self.work_contract.to_dict(),
            "required_artifacts": [item.to_dict() for item in self.required_artifacts],
            "optional_artifacts": [item.to_dict() for item in self.optional_artifacts],
            "budget": self.budget.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ContextBundle:
    bundle_id: str
    request_id: str
    goal_id: str
    todo_id: str
    items: tuple[ContextItem, ...]
    budget: ContextBudget
    required_artifact_ids: tuple[str, ...] = ()
    omitted_optional_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("bundle_id", "request_id", "goal_id", "todo_id"):
            _required_text(getattr(self, field_name), field_name)
        items = tuple(self.items)
        if any(not isinstance(item, ContextItem) for item in items):
            raise ValueError("items must contain only ContextItem values")
        item_ids = [item.item_id for item in items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("items must not contain duplicate item_id values")
        artifact_ids = [item.artifact_ref.artifact_id for item in items if item.artifact_ref is not None]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("items must not contain the same artifact more than once")
        object.__setattr__(self, "items", items)
        if not isinstance(self.budget, ContextBudget):
            raise ValueError("budget must be a ContextBudget")
        required_ids = _id_references(self.required_artifact_ids, "required_artifact_ids")
        omitted_ids = _id_references(self.omitted_optional_artifact_ids, "omitted_optional_artifact_ids")
        if set(required_ids) & set(omitted_ids):
            raise ValueError("required and omitted optional artifact IDs must not overlap")
        selected_artifacts = set(artifact_ids)
        missing_required = set(required_ids) - selected_artifacts
        if missing_required:
            raise ValueError(f"bundle is missing required artifacts: {sorted(missing_required)}")
        if set(omitted_ids) & selected_artifacts:
            raise ValueError("omitted optional artifacts must not also be selected")
        object.__setattr__(self, "required_artifact_ids", required_ids)
        object.__setattr__(self, "omitted_optional_artifact_ids", omitted_ids)
        if len(items) > self.budget.max_items:
            raise ValueError("context item count exceeds budget")
        if self.used_tokens > self.budget.max_tokens:
            raise ValueError("context token estimate exceeds budget")
        if self.used_bytes > self.budget.max_bytes:
            raise ValueError("context byte size exceeds budget")

    @property
    def used_tokens(self) -> int:
        return sum(item.token_estimate for item in self.items)

    @property
    def used_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "request_id": self.request_id,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "items": [item.to_dict() for item in self.items],
            "budget": self.budget.to_dict(),
            "used_tokens": self.used_tokens,
            "used_bytes": self.used_bytes,
            "required_artifact_ids": list(self.required_artifact_ids),
            "omitted_optional_artifact_ids": list(self.omitted_optional_artifact_ids),
        }
