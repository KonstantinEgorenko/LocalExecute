'Bounded model-visible projection for immutable recovery recovery evidence.'
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore

MAX_PROJECTION_BYTES = 3072


def _clip(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    encoded = text.encode("utf-8")[:limit]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            encoded = encoded[:exc.start]


def _texts(value: object, *, limit: int, item_limit: int | None = None) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else ()
    result = sorted({_clip(item, item_limit or limit) for item in values if isinstance(item, str) and item.strip()})
    return tuple(result[:limit])


@dataclass(frozen=True, slots=True)
class RecoveryContextProjectionV1:
    schema_version: int
    todo_id: str
    recovery_cycle: int
    failure_kind: str
    execution_outcome: str
    reason: str
    production_action: str
    diagnosis_action: str | None
    changed_paths: tuple[str, ...]
    implicated_files: tuple[str, ...]
    required_paths: tuple[str, ...]
    corrected_invariants: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    raw_recovery_artifact_id: str
    truncated_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "todo_id": self.todo_id,
            "recovery_cycle": self.recovery_cycle, "failure_kind": self.failure_kind,
            "execution_outcome": self.execution_outcome, "reason": self.reason,
            "production_action": self.production_action, "phase15_action": self.diagnosis_action,
            "changed_paths": list(self.changed_paths), "implicated_files": list(self.implicated_files),
            "required_paths": list(self.required_paths), "corrected_invariants": list(self.corrected_invariants),
            "evidence_ids": list(self.evidence_ids), "raw_recovery_artifact_id": self.raw_recovery_artifact_id,
            "truncated_fields": list(self.truncated_fields),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def projection_from_raw(*, raw_recovery_artifact_id: str, raw_content: str) -> RecoveryContextProjectionV1:
    try:
        outer = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("malformed durable raw recovery content") from exc
    if not isinstance(outer, Mapping):
        raise RuntimeError("durable raw recovery must be an object")
    recovery = outer.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    failure = recovery.get("deterministic_failure")
    failure = failure if isinstance(failure, Mapping) else {}
    diagnosis = recovery.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, Mapping) else {}
    truncations: list[str] = []
    def bounded(name: str, value: object, limit: int) -> str:
        original = value if isinstance(value, str) else ""
        clipped = _clip(original, limit)
        if clipped != original:
            truncations.append(name)
        return clipped
    def bounded_texts(name: str, value: object, count: int, width: int) -> tuple[str, ...]:
        original = value if isinstance(value, (list, tuple)) else ()
        result = _texts(original, limit=count, item_limit=width)
        if len(result) != len(set(item for item in original if isinstance(item, str) and item.strip())) or any(len(item.encode("utf-8")) > width for item in original if isinstance(item, str)):
            truncations.append(name)
        return result
    projection = RecoveryContextProjectionV1(
        1, bounded("todo_id", outer.get("todo_id", ""), 128), int(outer.get("cycle", 0)),
        bounded("failure_kind", failure.get("failure_kind", "UNKNOWN"), 64),
        bounded("execution_outcome", failure.get("execution_outcome", "UNKNOWN"), 96),
        bounded("reason", failure.get("reason", outer.get("reason", "")), 384),
        bounded("production_action", recovery.get("production_action", recovery.get("controller_action", "STOP")), 64),
        bounded("phase15_action", recovery.get("phase15_requested_action", recovery.get("controller_action", "")), 64) or None,
        bounded_texts("changed_paths", failure.get("changed_paths"), 8, 128),
        bounded_texts("implicated_files", diagnosis.get("implicated_files"), 8, 128),
        bounded_texts("required_paths", diagnosis.get("required_paths", diagnosis.get("target_files")), 8, 128),
        bounded_texts("corrected_invariants", diagnosis.get("corrected_invariants"), 3, 192),
        bounded_texts("evidence_ids", failure.get("evidence_ids"), 8, 128),
        bounded("raw_recovery_artifact_id", raw_recovery_artifact_id, 192), tuple(sorted(set(truncations))),
    )
    if not projection.todo_id or projection.recovery_cycle <= 0:
        raise RuntimeError("raw recovery identity is invalid")
    if len(projection.canonical_json().encode("utf-8")) > MAX_PROJECTION_BYTES:
        raise AssertionError("deterministic projection packing invariant violated")
    return projection


def persist_projection(store: ArtifactStore, projection: RecoveryContextProjectionV1):
    content = projection.canonical_json()
    artifact_id = f"phase21-3:recovery-projection:{projection.fingerprint[:32]}"
    artifact = Artifact.from_text(
        artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE, content=content, media_type="application/json",
        source="RecoveryContextProjectionV1", created_at="1970-01-01T00:00:00+00:00",
        metadata={"production_role": "RECOVERY_CONTEXT_PROJECTION", "todo_id": projection.todo_id,
                  "recovery_cycle": projection.recovery_cycle, "fingerprint": projection.fingerprint,
                  "raw_recovery_artifact_id": projection.raw_recovery_artifact_id},
    )
    if store.has_artifact(artifact_id):
        existing = store.get_artifact(artifact_id)
        if existing.content != content:
            raise RuntimeError("recovery projection identity conflict")
        return existing.ref
    return store.create_artifact(artifact).ref

# Version-one constructor/attribute compatibility; wire keys stay byte-stable.
from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(RecoveryContextProjectionV1, {'phase15_action': 'diagnosis_action'})
