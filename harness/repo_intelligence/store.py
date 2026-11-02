"""Durable create-only RepositoryIndex store."""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from .errors import RepositoryIndexNotFoundError, RepositoryIntegrityError
from .models import RepositoryIndex, canonical_json


class RepositoryIndexStore(ABC):
    @abstractmethod
    def put(self, index: RepositoryIndex) -> RepositoryIndex: ...
    @abstractmethod
    def get(self, index_id: str) -> RepositoryIndex: ...
    @abstractmethod
    def has(self, index_id: str) -> bool: ...


class InMemoryRepositoryIndexStore(RepositoryIndexStore):
    def __init__(self) -> None: self._values: dict[str, RepositoryIndex] = {}
    def put(self, index: RepositoryIndex) -> RepositoryIndex:
        if not isinstance(index, RepositoryIndex): raise RepositoryIntegrityError("index must be RepositoryIndex")
        previous = self._values.get(index.index_id)
        if previous is not None and previous != index: raise RepositoryIntegrityError("repository index identity conflict")
        self._values[index.index_id] = index
        return index
    def get(self, index_id: str) -> RepositoryIndex:
        try: return self._values[index_id]
        except KeyError as exc: raise RepositoryIndexNotFoundError(f"missing repository index: {index_id}") from exc
    def has(self, index_id: str) -> bool: return index_id in self._values


class FileRepositoryIndexStore(RepositoryIndexStore):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, index_id: str) -> Path:
        if not isinstance(index_id, str) or not index_id.startswith("repo-index-") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in index_id):
            raise RepositoryIntegrityError("invalid repository index_id")
        return self.root / f"{index_id}.json"

    def put(self, index: RepositoryIndex) -> RepositoryIndex:
        path = self._path(index.index_id)
        payload = canonical_json(index.to_dict()) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") != payload: raise RepositoryIntegrityError("repository index identity conflict")
            return index
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=self.root, delete=False) as handle:
                tmp_name = handle.name; handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if tmp_name and os.path.exists(tmp_name): os.unlink(tmp_name)
        return index

    def get(self, index_id: str) -> RepositoryIndex:
        path = self._path(index_id)
        if not path.is_file(): raise RepositoryIndexNotFoundError(f"missing repository index: {index_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict): raise ValueError("payload must be object")
            return RepositoryIndex.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RepositoryIntegrityError(f"invalid persisted repository index: {exc}") from exc

    def has(self, index_id: str) -> bool:
        try: return self._path(index_id).is_file()
        except RepositoryIntegrityError: return False
