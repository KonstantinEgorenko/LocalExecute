'Immutable repository intelligence repository intelligence domain models.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value


def _non_negative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _positive(value: object, name: str) -> int:
    result = _non_negative(value, name)
    if result == 0:
        raise ValueError(f"{name} must be > 0")
    return result


def _sha(value: object, name: str) -> str:
    value = _text(value, name).lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be SHA-256 hex")
    return value


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def stable_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RepositoryFileKind(StrEnum):
    SOURCE = "SOURCE"
    TEST = "TEST"
    CONFIG = "CONFIG"
    DOCUMENTATION = "DOCUMENTATION"
    OTHER = "OTHER"


class RepositorySymbolKind(StrEnum):
    CLASS = "CLASS"
    FUNCTION = "FUNCTION"
    METHOD = "METHOD"


@dataclass(frozen=True, slots=True)
class RepositoryIntelligencePolicy:
    max_files: int = 5000
    max_file_bytes: int = 524_288
    max_total_bytes: int = 33_554_432
    max_symbols_per_file: int = 2000
    max_results: int = 24
    max_context_files: int = 12
    max_context_bytes: int = 262_144
    excluded_directories: tuple[str, ...] = (
        ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", "node_modules", "dist", "build",
    )
    trusted_worktrees_only: bool = True
    enforce_no_workspace_mutation: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_files", "max_file_bytes", "max_total_bytes", "max_symbols_per_file",
            "max_results", "max_context_files", "max_context_bytes",
        ):
            _positive(getattr(self, name), name)
        if self.max_context_files > self.max_results:
            raise ValueError("max_context_files must not exceed max_results")
        if self.max_context_bytes > self.max_total_bytes:
            raise ValueError("max_context_bytes must not exceed max_total_bytes")
        excluded = _unique_text(self.excluded_directories, "excluded_directories")
        if any("/" in item or "\\" in item or item in (".", "..") for item in excluded):
            raise ValueError("excluded_directories must contain directory names, not paths")
        object.__setattr__(self, "excluded_directories", excluded)
        if self.trusted_worktrees_only is not True:
            raise ValueError("trusted_worktrees_only must be true")
        if self.enforce_no_workspace_mutation is not True:
            raise ValueError("enforce_no_workspace_mutation must be true")

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "RepositoryIntelligencePolicy":
        raw = policies.get("repository_intelligence", {})
        if not isinstance(raw, Mapping):
            raise ValueError("repository_intelligence policy must be mapping")
        return cls(
            max_files=raw.get("max_files", 5000),
            max_file_bytes=raw.get("max_file_bytes", 524_288),
            max_total_bytes=raw.get("max_total_bytes", 33_554_432),
            max_symbols_per_file=raw.get("max_symbols_per_file", 2000),
            max_results=raw.get("max_results", 24),
            max_context_files=raw.get("max_context_files", 12),
            max_context_bytes=raw.get("max_context_bytes", 262_144),
            excluded_directories=tuple(raw.get("excluded_directories", (".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build"))),
            trusted_worktrees_only=raw.get("trusted_worktrees_only", True),
            enforce_no_workspace_mutation=raw.get("enforce_no_workspace_mutation", True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_symbols_per_file": self.max_symbols_per_file,
            "max_results": self.max_results,
            "max_context_files": self.max_context_files,
            "max_context_bytes": self.max_context_bytes,
            "excluded_directories": list(self.excluded_directories),
            "trusted_worktrees_only": self.trusted_worktrees_only,
            "enforce_no_workspace_mutation": self.enforce_no_workspace_mutation,
        }


@dataclass(frozen=True, slots=True)
class RepositorySymbol:
    name: str
    qualified_name: str
    kind: RepositorySymbolKind
    line: int
    end_line: int

    def __post_init__(self) -> None:
        _text(self.name, "name"); _text(self.qualified_name, "qualified_name")
        if not isinstance(self.kind, RepositorySymbolKind): raise ValueError("kind must be RepositorySymbolKind")
        _positive(self.line, "line"); _positive(self.end_line, "end_line")
        if self.end_line < self.line: raise ValueError("end_line must be >= line")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "qualified_name": self.qualified_name, "kind": self.kind.value, "line": self.line, "end_line": self.end_line}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositorySymbol":
        return cls(str(raw["name"]), str(raw["qualified_name"]), RepositorySymbolKind(str(raw["kind"])), int(raw["line"]), int(raw["end_line"]))


@dataclass(frozen=True, slots=True)
class RepositoryImport:
    module: str
    names: tuple[str, ...] = ()
    level: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.module, str): raise ValueError("module must be string")
        object.__setattr__(self, "names", _unique_text(self.names, "names"))
        _non_negative(self.level, "level")
        if not self.module and not self.names: raise ValueError("import must have module or names")

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "names": list(self.names), "level": self.level}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryImport":
        return cls(str(raw.get("module", "")), tuple(str(x) for x in raw.get("names", ())), int(raw.get("level", 0)))


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    kind: RepositoryFileKind
    language: str
    size_bytes: int
    sha256: str
    symbols: tuple[RepositorySymbol, ...] = ()
    imports: tuple[RepositoryImport, ...] = ()
    parse_error: str | None = None

    def __post_init__(self) -> None:
        path = _text(self.path, "path").replace("\\", "/")
        if path.startswith("/") or path.startswith("../") or "/../" in f"/{path}/": raise ValueError("path must be safe repository-relative path")
        object.__setattr__(self, "path", path)
        if not isinstance(self.kind, RepositoryFileKind): raise ValueError("kind must be RepositoryFileKind")
        _text(self.language, "language"); _non_negative(self.size_bytes, "size_bytes"); _sha(self.sha256, "sha256")
        symbols = tuple(self.symbols); imports = tuple(self.imports)
        if any(not isinstance(x, RepositorySymbol) for x in symbols): raise ValueError("symbols must contain RepositorySymbol")
        if any(not isinstance(x, RepositoryImport) for x in imports): raise ValueError("imports must contain RepositoryImport")
        object.__setattr__(self, "symbols", symbols); object.__setattr__(self, "imports", imports)
        if self.parse_error is not None and not isinstance(self.parse_error, str): raise ValueError("parse_error must be str|None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "kind": self.kind.value, "language": self.language,
            "size_bytes": self.size_bytes, "sha256": self.sha256,
            "symbols": [x.to_dict() for x in self.symbols], "imports": [x.to_dict() for x in self.imports],
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryFile":
        return cls(
            path=str(raw["path"]), kind=RepositoryFileKind(str(raw["kind"])), language=str(raw["language"]),
            size_bytes=int(raw["size_bytes"]), sha256=str(raw["sha256"]),
            symbols=tuple(RepositorySymbol.from_dict(x) for x in raw.get("symbols", ())),
            imports=tuple(RepositoryImport.from_dict(x) for x in raw.get("imports", ())),
            parse_error=raw.get("parse_error"),
        )


@dataclass(frozen=True, slots=True)
class RepositoryDependencyEdge:
    source_path: str
    target_path: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.source_path, "source_path"); _text(self.target_path, "target_path"); _text(self.reason, "reason")
        if self.source_path == self.target_path: raise ValueError("dependency edge cannot self-reference")

    def to_dict(self) -> dict[str, str]: return {"source_path": self.source_path, "target_path": self.target_path, "reason": self.reason}
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryDependencyEdge": return cls(str(raw["source_path"]), str(raw["target_path"]), str(raw["reason"]))


@dataclass(frozen=True, slots=True)
class RepositoryTestAssociation:
    test_path: str
    source_path: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.test_path, "test_path"); _text(self.source_path, "source_path"); _text(self.reason, "reason")
        if self.test_path == self.source_path: raise ValueError("test association cannot self-reference")

    def to_dict(self) -> dict[str, str]: return {"test_path": self.test_path, "source_path": self.source_path, "reason": self.reason}
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryTestAssociation": return cls(str(raw["test_path"]), str(raw["source_path"]), str(raw["reason"]))


@dataclass(frozen=True, slots=True)
class RepositoryIndex:
    index_id: str
    schema_version: int
    lease_id: str
    repo_root: str
    head_commit: str
    diff_sha256: str
    dirty_paths: tuple[str, ...]
    files: tuple[RepositoryFile, ...]
    dependency_edges: tuple[RepositoryDependencyEdge, ...]
    test_associations: tuple[RepositoryTestAssociation, ...]
    skipped_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.index_id, "index_id"); _positive(self.schema_version, "schema_version"); _text(self.lease_id, "lease_id"); _text(self.repo_root, "repo_root")
        _text(self.head_commit, "head_commit"); _sha(self.diff_sha256, "diff_sha256")
        object.__setattr__(self, "dirty_paths", _unique_text(self.dirty_paths, "dirty_paths"))
        files = tuple(self.files); edges = tuple(self.dependency_edges); assoc = tuple(self.test_associations)
        if any(not isinstance(x, RepositoryFile) for x in files): raise ValueError("files must contain RepositoryFile")
        paths = [x.path for x in files]
        if len(set(paths)) != len(paths): raise ValueError("files must have unique paths")
        if any(not isinstance(x, RepositoryDependencyEdge) for x in edges): raise ValueError("dependency_edges invalid")
        if any(not isinstance(x, RepositoryTestAssociation) for x in assoc): raise ValueError("test_associations invalid")
        object.__setattr__(self, "files", files); object.__setattr__(self, "dependency_edges", edges); object.__setattr__(self, "test_associations", assoc)
        object.__setattr__(self, "skipped_paths", _unique_text(self.skipped_paths, "skipped_paths"))

    @property
    def file_map(self) -> Mapping[str, RepositoryFile]:
        return MappingProxyType({x.path: x for x in self.files})

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "lease_id": self.lease_id, "repo_root": self.repo_root,
            "head_commit": self.head_commit, "diff_sha256": self.diff_sha256, "dirty_paths": list(self.dirty_paths),
            "files": [x.to_dict() for x in self.files], "dependency_edges": [x.to_dict() for x in self.dependency_edges],
            "test_associations": [x.to_dict() for x in self.test_associations], "skipped_paths": list(self.skipped_paths),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_payload(); payload["index_id"] = self.index_id; return payload

    @classmethod
    def create(cls, *, lease_id: str, repo_root: str, head_commit: str, diff_sha256: str, dirty_paths: tuple[str, ...], files: tuple[RepositoryFile, ...], dependency_edges: tuple[RepositoryDependencyEdge, ...], test_associations: tuple[RepositoryTestAssociation, ...], skipped_paths: tuple[str, ...] = ()) -> "RepositoryIndex":
        seed = cls("pending", 1, lease_id, repo_root, head_commit, diff_sha256, dirty_paths, files, dependency_edges, test_associations, skipped_paths)
        return cls(f"repo-index-{stable_sha256(seed.identity_payload())[:24]}", 1, lease_id, repo_root, head_commit, diff_sha256, dirty_paths, files, dependency_edges, test_associations, skipped_paths)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryIndex":
        value = cls(
            index_id=str(raw["index_id"]), schema_version=int(raw["schema_version"]), lease_id=str(raw["lease_id"]), repo_root=str(raw["repo_root"]),
            head_commit=str(raw["head_commit"]), diff_sha256=str(raw["diff_sha256"]), dirty_paths=tuple(str(x) for x in raw.get("dirty_paths", ())),
            files=tuple(RepositoryFile.from_dict(x) for x in raw.get("files", ())),
            dependency_edges=tuple(RepositoryDependencyEdge.from_dict(x) for x in raw.get("dependency_edges", ())),
            test_associations=tuple(RepositoryTestAssociation.from_dict(x) for x in raw.get("test_associations", ())),
            skipped_paths=tuple(str(x) for x in raw.get("skipped_paths", ())),
        )
        expected = RepositoryIndex.create(lease_id=value.lease_id, repo_root=value.repo_root, head_commit=value.head_commit, diff_sha256=value.diff_sha256, dirty_paths=value.dirty_paths, files=value.files, dependency_edges=value.dependency_edges, test_associations=value.test_associations, skipped_paths=value.skipped_paths)
        if expected.index_id != value.index_id: raise ValueError("repository index_id integrity mismatch")
        return value


@dataclass(frozen=True, slots=True)
class RepositoryRetrievalHit:
    path: str
    score: int
    reasons: tuple[str, ...]
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _text(self.path, "path"); _non_negative(self.score, "score"); _non_negative(self.size_bytes, "size_bytes"); _sha(self.sha256, "sha256")
        object.__setattr__(self, "reasons", _unique_text(self.reasons, "reasons"))
        if self.score == 0: raise ValueError("retrieval hit score must be > 0")

    def to_dict(self) -> dict[str, Any]: return {"path": self.path, "score": self.score, "reasons": list(self.reasons), "size_bytes": self.size_bytes, "sha256": self.sha256}
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryRetrievalHit": return cls(str(raw["path"]), int(raw["score"]), tuple(str(x) for x in raw.get("reasons", ())), int(raw["size_bytes"]), str(raw["sha256"]))


@dataclass(frozen=True, slots=True)
class RepositoryRetrievalResult:
    retrieval_id: str
    index_id: str
    query: str
    explicit_paths: tuple[str, ...]
    hits: tuple[RepositoryRetrievalHit, ...]
    total_bytes: int

    def __post_init__(self) -> None:
        _text(self.retrieval_id, "retrieval_id"); _text(self.index_id, "index_id"); _text(self.query, "query")
        object.__setattr__(self, "explicit_paths", _unique_text(self.explicit_paths, "explicit_paths"))
        hits = tuple(self.hits)
        if any(not isinstance(x, RepositoryRetrievalHit) for x in hits): raise ValueError("hits invalid")
        if len({x.path for x in hits}) != len(hits): raise ValueError("hits paths must be unique")
        object.__setattr__(self, "hits", hits); _non_negative(self.total_bytes, "total_bytes")
        if sum(x.size_bytes for x in hits) != self.total_bytes: raise ValueError("total_bytes mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {"index_id": self.index_id, "query": self.query, "explicit_paths": list(self.explicit_paths), "hits": [x.to_dict() for x in self.hits], "total_bytes": self.total_bytes}

    def to_dict(self) -> dict[str, Any]: payload=self.identity_payload(); payload["retrieval_id"] = self.retrieval_id; return payload

    @classmethod
    def create(cls, *, index_id: str, query: str, explicit_paths: tuple[str, ...], hits: tuple[RepositoryRetrievalHit, ...]) -> "RepositoryRetrievalResult":
        total = sum(x.size_bytes for x in hits)
        seed = {"index_id": index_id, "query": query, "explicit_paths": list(explicit_paths), "hits": [x.to_dict() for x in hits], "total_bytes": total}
        return cls(f"repo-retrieval-{stable_sha256(seed)[:24]}", index_id, query, explicit_paths, hits, total)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositoryRetrievalResult":
        value = cls(str(raw["retrieval_id"]), str(raw["index_id"]), str(raw["query"]), tuple(str(x) for x in raw.get("explicit_paths", ())), tuple(RepositoryRetrievalHit.from_dict(x) for x in raw.get("hits", ())), int(raw["total_bytes"]))
        expected = cls.create(index_id=value.index_id, query=value.query, explicit_paths=value.explicit_paths, hits=value.hits)
        if expected.retrieval_id != value.retrieval_id: raise ValueError("retrieval_id integrity mismatch")
        return value
