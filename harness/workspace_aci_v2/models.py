"""Strict model-facing ACI contracts for Phase 24E."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class AciActionKindV2(StrEnum):
    VIEW = "VIEW"
    SEARCH = "SEARCH"
    EDIT = "EDIT"
    RUN = "RUN"


class AciErrorCodeV2(StrEnum):
    INVALID_ACTION = "INVALID_ACTION"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    PATH_NOT_VIEWABLE = "PATH_NOT_VIEWABLE"
    EXACT_REPLACEMENT_REQUIRED = "EXACT_REPLACEMENT_REQUIRED"
    OCCURRENCE_MISMATCH = "OCCURRENCE_MISMATCH"
    NO_EFFECT = "NO_EFFECT"
    SYNTAX_PREFLIGHT_FAILED = "SYNTAX_PREFLIGHT_FAILED"
    LOW_LEVEL_DENIED = "LOW_LEVEL_DENIED"
    LOW_LEVEL_CONFLICT = "LOW_LEVEL_CONFLICT"
    LOW_LEVEL_INVALID = "LOW_LEVEL_INVALID"
    LOW_LEVEL_TIMEOUT = "LOW_LEVEL_TIMEOUT"
    LOW_LEVEL_ERROR = "LOW_LEVEL_ERROR"


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class AciActionV2:
    kind: AciActionKindV2
    path: str = ""
    query: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    expected_occurrences: int = 1
    argv: tuple[str, ...] = ()
    cwd: str = ""
    timeout_seconds: int = 120
    case_sensitive: bool = True
    # Internal observed-version binding; never supplied by the model wire schema.
    expected_observed_content: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AciActionKindV2):
            raise ValueError("kind must be AciActionKindV2")
        if not isinstance(self.path, str):
            raise ValueError("path must be a string")
        if not isinstance(self.cwd, str):
            raise ValueError("cwd must be a string")
        if not isinstance(self.case_sensitive, bool):
            raise ValueError("case_sensitive must be bool")
        if (
            not isinstance(self.expected_occurrences, int)
            or isinstance(self.expected_occurrences, bool)
            or self.expected_occurrences <= 0
        ):
            raise ValueError("expected_occurrences must be an integer > 0")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be an integer > 0")
        object.__setattr__(self, "argv", tuple(self.argv))
        if self.expected_observed_content is not None:
            if not isinstance(self.expected_observed_content, str):
                raise ValueError("expected_observed_content must be a string or null")
            if self.kind is not AciActionKindV2.EDIT or self.old_text is None:
                raise ValueError("observed content binding requires an existing-file EDIT")

        if self.kind is AciActionKindV2.VIEW:
            if self.query is not None or self.old_text is not None or self.new_text is not None or self.argv:
                raise ValueError("VIEW accepts only path")
        elif self.kind is AciActionKindV2.SEARCH:
            _text(self.query, "query")
            if self.old_text is not None or self.new_text is not None or self.argv:
                raise ValueError("SEARCH does not accept edit/run fields")
        elif self.kind is AciActionKindV2.EDIT:
            _text(self.path, "path")
            _text(self.new_text, "new_text", allow_empty=True)
            if self.old_text is not None and not isinstance(self.old_text, str):
                raise ValueError("old_text must be a string or null")
            if self.query is not None or self.argv:
                raise ValueError("EDIT does not accept search/run fields")
        elif self.kind is AciActionKindV2.RUN:
            if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
                raise ValueError("RUN argv must be a non-empty tuple of strings")
            if self.query is not None or self.old_text is not None or self.new_text is not None:
                raise ValueError("RUN does not accept search/edit fields")


@dataclass(frozen=True, slots=True)
class AciObservationV2:
    action: AciActionKindV2
    ok: bool
    output: str
    metadata: Mapping[str, Any]
    changed_paths: tuple[str, ...] = ()
    error_code: AciErrorCodeV2 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, AciActionKindV2):
            raise ValueError("action must be AciActionKindV2")
        if not isinstance(self.ok, bool):
            raise ValueError("ok must be bool")
        if not isinstance(self.output, str):
            raise ValueError("output must be string")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        if self.ok and self.error_code is not None:
            raise ValueError("successful observation cannot contain error_code")
        if not self.ok and self.error_code is None:
            raise ValueError("failed observation requires error_code")
