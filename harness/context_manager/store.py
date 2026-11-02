"""Deterministic in-memory and durable content-addressed Artifact stores."""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .errors import (
    ArtifactIdentityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStoreCorruptionError,
)
from .models import Artifact, ArtifactKind, ArtifactOrigin, ArtifactRef

_SCHEMA_VERSION = 1
_INDEX_KEYS = {"schema_version", "artifacts"}
_RECORD_KEYS = {
    "artifact_id",
    "kind",
    "origin",
    "sha256",
    "size_bytes",
    "media_type",
    "source",
    "created_at",
    "scope_tags",
    "metadata",
}


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ArtifactIntegrityError(f"artifact metadata is not JSON-safe: {exc}") from exc


def _valid_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ArtifactStoreCorruptionError("artifact sha256 must be a lowercase 64-character hex digest")
    return value


def _record_from_artifact(artifact: Artifact) -> dict[str, Any]:
    payload = artifact.to_dict()
    payload.pop("content")
    return _copy_json(payload)


def _artifact_from_record(record: object, content: str) -> Artifact:
    if not isinstance(record, dict):
        raise ArtifactStoreCorruptionError("artifact index record must be an object")
    if set(record) != _RECORD_KEYS:
        raise ArtifactStoreCorruptionError("artifact index record keys must match schema exactly")
    try:
        return Artifact(
            artifact_id=record["artifact_id"],
            kind=ArtifactKind(record["kind"]),
            origin=ArtifactOrigin(record["origin"]),
            content=content,
            sha256=record["sha256"],
            size_bytes=record["size_bytes"],
            media_type=record["media_type"],
            source=record["source"],
            created_at=record["created_at"],
            scope_tags=tuple(record["scope_tags"]),
            metadata=record["metadata"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactStoreCorruptionError(f"invalid artifact metadata: {exc}") from exc


class ArtifactStore(ABC):
    """Create-only artifact identity store."""

    @abstractmethod
    def create_artifact(self, artifact: Artifact) -> Artifact:
        ...

    @abstractmethod
    def get_artifact(self, artifact_id: str) -> Artifact:
        ...

    @abstractmethod
    def get_ref(self, artifact_id: str) -> ArtifactRef:
        ...

    @abstractmethod
    def has_artifact(self, artifact_id: str) -> bool:
        ...

    @abstractmethod
    def list_refs(self) -> tuple[ArtifactRef, ...]:
        ...


class InMemoryArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    @staticmethod
    def _validate_artifact(artifact: Artifact) -> None:
        if not isinstance(artifact, Artifact):
            raise ArtifactIntegrityError("artifact must be an Artifact")

    def create_artifact(self, artifact: Artifact) -> Artifact:
        self._validate_artifact(artifact)
        if artifact.artifact_id in self._artifacts:
            raise ArtifactIdentityError(f"duplicate artifact_id: {artifact.artifact_id}")
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ArtifactNotFoundError("artifact_id must be a non-empty string")
        try:
            return self._artifacts[artifact_id]
        except KeyError as exc:
            raise ArtifactNotFoundError(f"missing artifact_id: {artifact_id}") from exc

    def get_ref(self, artifact_id: str) -> ArtifactRef:
        return self.get_artifact(artifact_id).ref

    def has_artifact(self, artifact_id: str) -> bool:
        return isinstance(artifact_id, str) and bool(artifact_id.strip()) and artifact_id in self._artifacts

    def list_refs(self) -> tuple[ArtifactRef, ...]:
        return tuple(self._artifacts[key].ref for key in sorted(self._artifacts))


class FileArtifactStore(InMemoryArtifactStore):
    """Durable artifact store with content-addressed UTF-8 object blobs.

    Layout::

        <root>/index.json
        <root>/objects/<sha256>

    Artifact identities are create-only. Different artifact IDs may point to the
    same SHA-256 object, which physically deduplicates identical content.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.objects_root = self.root / "objects"
        self.index_path = self.root / "index.json"
        super().__init__()
        self._load()

    def _object_path(self, sha256: str) -> Path:
        return self.objects_root / sha256

    def _atomic_write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                delete=False,
            ) as handle:
                tmp_name = handle.name
                json.dump(payload, handle, sort_keys=True, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if tmp_name is not None and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                tmp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if tmp_name is not None and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _index_payload(self, artifacts: dict[str, Artifact] | None = None) -> dict[str, Any]:
        values = self._artifacts if artifacts is None else artifacts
        return {
            "schema_version": _SCHEMA_VERSION,
            "artifacts": [_record_from_artifact(values[key]) for key in sorted(values)],
        }

    def _read_object(self, sha256: str, size_bytes: int) -> str:
        path = self._object_path(sha256)
        if not path.is_file():
            raise ArtifactStoreCorruptionError(f"missing object for sha256: {sha256}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreCorruptionError(f"cannot read object {sha256}: {exc}") from exc
        if len(payload) != size_bytes:
            raise ArtifactIntegrityError(f"object size mismatch for sha256: {sha256}")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactIntegrityError(f"object is not valid UTF-8 for sha256: {sha256}") from exc
        # Artifact construction performs the authoritative SHA-256 check.
        return content

    def _ensure_object(self, artifact: Artifact) -> None:
        path = self._object_path(artifact.sha256)
        payload = artifact.content.encode("utf-8")
        if path.exists():
            existing = self._read_object(artifact.sha256, artifact.size_bytes)
            if existing != artifact.content:
                raise ArtifactIntegrityError(f"existing object content mismatch for sha256: {artifact.sha256}")
            return
        self._atomic_write_bytes(path, payload)

    def create_artifact(self, artifact: Artifact) -> Artifact:
        self._validate_artifact(artifact)
        if artifact.artifact_id in self._artifacts:
            raise ArtifactIdentityError(f"duplicate artifact_id: {artifact.artifact_id}")

        # Persist content first. If metadata persistence fails, the only possible
        # residue is an unreferenced content-addressed object, never a partial ID.
        self._ensure_object(artifact)
        candidate = dict(self._artifacts)
        candidate[artifact.artifact_id] = artifact
        try:
            self._atomic_write_json(self.index_path, self._index_payload(candidate))
        except (OSError, TypeError, ValueError) as exc:
            raise ArtifactStoreCorruptionError(f"failed to persist artifact index: {exc}") from exc
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArtifactStoreCorruptionError(f"malformed artifact index: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != _INDEX_KEYS:
            raise ArtifactStoreCorruptionError("artifact index keys must match schema exactly")
        if raw.get("schema_version") != _SCHEMA_VERSION:
            raise ArtifactStoreCorruptionError("unsupported artifact index schema_version")
        records = raw.get("artifacts")
        if not isinstance(records, list):
            raise ArtifactStoreCorruptionError("artifact index artifacts must be a list")

        loaded: dict[str, Artifact] = {}
        for record in records:
            if not isinstance(record, dict):
                raise ArtifactStoreCorruptionError("artifact index record must be an object")
            if set(record) != _RECORD_KEYS:
                raise ArtifactStoreCorruptionError("artifact index record keys must match schema exactly")
            artifact_id = record.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ArtifactStoreCorruptionError("artifact_id must be a non-empty string")
            if artifact_id in loaded:
                raise ArtifactStoreCorruptionError(f"duplicate artifact identity in index: {artifact_id}")
            sha256 = _valid_sha256(record.get("sha256"))
            size_bytes = record.get("size_bytes")
            if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
                raise ArtifactStoreCorruptionError("artifact size_bytes must be a non-negative integer")
            content = self._read_object(sha256, size_bytes)
            artifact = _artifact_from_record(record, content)
            loaded[artifact_id] = artifact

        self._artifacts = loaded
