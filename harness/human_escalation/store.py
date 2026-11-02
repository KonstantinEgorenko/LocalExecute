'Durable JSON store for research campaign.R0 human requests.'
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .models import HumanRequestStatusV1, HumanRequestV1, _safe_id, stable_sha256


class HumanRequestStoreError(RuntimeError):
    pass


class HumanRequestNotFoundError(HumanRequestStoreError):
    pass


class HumanRequestGenerationConflict(HumanRequestStoreError):
    pass


class HumanRequestResolutionConflict(HumanRequestStoreError):
    pass


class HumanRequestCorruptStateError(HumanRequestStoreError):
    pass


class JsonHumanRequestStore:
    """Mission-scoped, atomic durable request ledger.

    Human requests are expected to be low-volume compared with experiment data,
    so one bounded ledger per mission keeps duplicate suppression and resolution
    transactional without introducing another service/database authority.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, mission_id: str) -> Path:
        return self.root / _safe_id(mission_id, "mission_id")

    def _path(self, mission_id: str) -> Path:
        return self._dir(mission_id) / "human_requests.json"

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".tmp-{os.getpid()}")
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    @staticmethod
    def _empty_payload(mission_id: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": mission_id,
            "generation": -1,
            "requests": [],
        }

    @staticmethod
    def _with_fingerprint(payload: Mapping[str, Any]) -> dict[str, Any]:
        plain = dict(payload)
        plain["state_fingerprint"] = stable_sha256(plain)
        return plain

    def _read_payload(self, mission_id: str, *, missing_ok: bool = False) -> dict[str, Any]:
        path = self._path(mission_id)
        if not path.exists():
            if missing_ok:
                return self._empty_payload(mission_id)
            raise HumanRequestNotFoundError(mission_id)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HumanRequestCorruptStateError(f"cannot read human request state {path}: {exc}") from exc
        if not isinstance(stored, dict):
            raise HumanRequestCorruptStateError(f"human request state must be JSON object: {path}")
        fingerprint = stored.pop("state_fingerprint", None)
        if not isinstance(fingerprint, str) or fingerprint != stable_sha256(stored):
            raise HumanRequestCorruptStateError(f"human request state fingerprint mismatch: {mission_id}")
        if stored.get("schema_version") != 1:
            raise HumanRequestCorruptStateError("human request store schema_version must be 1")
        if stored.get("mission_id") != mission_id:
            raise HumanRequestCorruptStateError("human request store mission_id mismatch")
        generation = stored.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise HumanRequestCorruptStateError("human request store generation must be integer >= 0")
        raw_requests = stored.get("requests")
        if not isinstance(raw_requests, list):
            raise HumanRequestCorruptStateError("human request store requests must be list")
        seen_ids: set[str] = set()
        try:
            requests = [HumanRequestV1.from_dict(raw) for raw in raw_requests]
        except (KeyError, TypeError, ValueError) as exc:
            raise HumanRequestCorruptStateError(f"invalid human request state: {exc}") from exc
        for request in requests:
            if request.mission_id != mission_id:
                raise HumanRequestCorruptStateError("request mission_id mismatch")
            if request.request_id in seen_ids:
                raise HumanRequestCorruptStateError("duplicate request_id in durable state")
            seen_ids.add(request.request_id)
        stored["requests"] = requests
        return stored

    @staticmethod
    def _serialize_payload(mission_id: str, generation: int, requests: tuple[HumanRequestV1, ...]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": mission_id,
            "generation": generation,
            "requests": [request.to_dict() for request in requests],
        }

    def generation(self, mission_id: str) -> int:
        return int(self._read_payload(mission_id)["generation"])

    def list_requests(self, mission_id: str) -> tuple[HumanRequestV1, ...]:
        payload = self._read_payload(mission_id)
        return tuple(payload["requests"])

    def get(self, mission_id: str, request_id: str) -> HumanRequestV1:
        _safe_id(request_id, "request_id")
        for request in self.list_requests(mission_id):
            if request.request_id == request_id:
                return request
        raise HumanRequestNotFoundError(f"mission={mission_id} request={request_id}")

    def list_pending(self, mission_id: str | None = None) -> tuple[HumanRequestV1, ...]:
        if mission_id is not None:
            return tuple(request for request in self.list_requests(mission_id) if not request.is_terminal)
        pending: list[HumanRequestV1] = []
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            path = directory / "human_requests.json"
            if not path.exists():
                continue
            pending.extend(request for request in self.list_requests(directory.name) if not request.is_terminal)
        return tuple(pending)

    def create(self, request: HumanRequestV1, *, expected_generation: int | None = None) -> HumanRequestV1:
        if not isinstance(request, HumanRequestV1):
            raise HumanRequestStoreError("request must be HumanRequestV1")
        if request.is_terminal:
            raise HumanRequestStoreError("new human request must be PENDING")
        payload = self._read_payload(request.mission_id, missing_ok=True)
        current_generation = int(payload["generation"])
        if expected_generation is not None and current_generation != expected_generation:
            raise HumanRequestGenerationConflict(
                f"mission={request.mission_id} expected_generation={expected_generation} actual={current_generation}"
            )
        existing = tuple(payload["requests"])
        for item in existing:
            if item.request_id == request.request_id:
                if item == request:
                    return item
                raise HumanRequestStoreError(f"duplicate request_id with different payload: {request.request_id}")
            if not item.is_terminal and item.deduplication_fingerprint == request.deduplication_fingerprint:
                return item
        next_generation = current_generation + 1
        serialized = self._serialize_payload(request.mission_id, next_generation, existing + (request,))
        self._atomic_write(self._path(request.mission_id), self._with_fingerprint(serialized))
        return request

    def resolve(
        self,
        mission_id: str,
        request_id: str,
        *,
        status: HumanRequestStatusV1,
        resolved_at: str,
        resolution_payload: Mapping[str, Any] | None = None,
        expected_generation: int | None = None,
    ) -> HumanRequestV1:
        _safe_id(mission_id, "mission_id")
        _safe_id(request_id, "request_id")
        if status is HumanRequestStatusV1.PENDING:
            raise HumanRequestStoreError("resolve status must be terminal")
        payload = self._read_payload(mission_id)
        current_generation = int(payload["generation"])
        if expected_generation is not None and current_generation != expected_generation:
            raise HumanRequestGenerationConflict(
                f"mission={mission_id} expected_generation={expected_generation} actual={current_generation}"
            )
        requests = list(payload["requests"])
        position = next((index for index, item in enumerate(requests) if item.request_id == request_id), None)
        if position is None:
            raise HumanRequestNotFoundError(f"mission={mission_id} request={request_id}")
        current = requests[position]
        candidate = replace(
            current,
            status=status,
            resolution_payload={} if resolution_payload is None else resolution_payload,
            resolved_at=resolved_at,
        )
        if current.is_terminal:
            if current == candidate:
                return current
            raise HumanRequestResolutionConflict(f"request already terminal: {request_id}")
        requests[position] = candidate
        serialized = self._serialize_payload(mission_id, current_generation + 1, tuple(requests))
        self._atomic_write(self._path(mission_id), self._with_fingerprint(serialized))
        return candidate
