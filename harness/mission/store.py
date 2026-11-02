'Atomic durable JSON journal for mission missions.'
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .models import MissionCheckpointV1, MissionReportV1, _safe_id, stable_sha256


class MissionStoreError(RuntimeError):
    pass


class MissionAlreadyExistsError(MissionStoreError):
    pass


class MissionNotFoundError(MissionStoreError):
    pass


class MissionGenerationConflict(MissionStoreError):
    pass


class MissionCorruptStateError(MissionStoreError):
    pass


class JsonMissionStore:
    'One directory per mission, atomic replace + generation CAS.\n\n    The journal is intentionally independent from model context. A process may\n    disappear while workflow is running; the last committed checkpoint remains\n    the source of truth and the next process resumes from it.\n    '

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, mission_id: str) -> Path:
        return self.root / _safe_id(mission_id, "mission_id")

    def _checkpoint_path(self, mission_id: str) -> Path:
        return self._dir(mission_id) / "mission.json"

    def _report_path(self, mission_id: str) -> Path:
        return self._dir(mission_id) / "report.json"

    @staticmethod
    def _read_json(path: Path) -> Mapping[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MissionNotFoundError(str(path)) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise MissionCorruptStateError(f"cannot read durable mission state {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise MissionCorruptStateError(f"mission state must be JSON object: {path}")
        return raw

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
    def _checkpoint_payload(checkpoint: MissionCheckpointV1) -> dict[str, Any]:
        raw = checkpoint.to_dict()
        raw["checkpoint_fingerprint"] = stable_sha256(raw)
        return raw

    def create(self, checkpoint: MissionCheckpointV1) -> None:
        path = self._checkpoint_path(checkpoint.mission_id)
        if path.exists():
            raise MissionAlreadyExistsError(checkpoint.mission_id)
        if checkpoint.generation != 0:
            raise MissionStoreError("new mission checkpoint generation must be 0")
        self._atomic_write(path, self._checkpoint_payload(checkpoint))

    def load(self, mission_id: str) -> MissionCheckpointV1:
        stored = dict(self._read_json(self._checkpoint_path(mission_id)))
        fingerprint = stored.pop("checkpoint_fingerprint", None)
        if not isinstance(fingerprint, str) or fingerprint != stable_sha256(stored):
            raise MissionCorruptStateError(f"mission checkpoint fingerprint mismatch: {mission_id}")
        try:
            return MissionCheckpointV1.from_dict(stored)
        except (KeyError, TypeError, ValueError) as exc:
            raise MissionCorruptStateError(f"invalid mission checkpoint {mission_id}: {exc}") from exc

    def save(self, checkpoint: MissionCheckpointV1, *, expected_generation: int) -> None:
        current = self.load(checkpoint.mission_id)
        if current.generation != expected_generation:
            raise MissionGenerationConflict(
                f"mission={checkpoint.mission_id} expected_generation={expected_generation} actual={current.generation}"
            )
        if checkpoint.generation != expected_generation + 1:
            raise MissionGenerationConflict(
                f"new generation must equal expected+1: {checkpoint.generation}!={expected_generation + 1}"
            )
        self._atomic_write(self._checkpoint_path(checkpoint.mission_id), self._checkpoint_payload(checkpoint))

    def write_report(self, report: MissionReportV1) -> str:
        path = self._report_path(report.mission_id)
        self._atomic_write(path, report.to_dict())
        return path.relative_to(self.root.parent).as_posix()

    def load_report(self, mission_id: str) -> MissionReportV1:
        raw = self._read_json(self._report_path(mission_id))
        try:
            return MissionReportV1.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise MissionCorruptStateError(f"invalid mission report {mission_id}: {exc}") from exc
