"""Durable, explicitly-unverified same-candidate recovery checkpoints."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin
from ..control_plane import Evidence, EvidenceStatus


def capture_replan_draft(workspace, lease, contract, responsibility):
    """Seal authorized raw draft bytes before retiring a changed responsibility.

    This is continuation evidence only. No verification or acceptance is copied.
    The existing RunStore and WorkspaceSessionFactory remain the authorities.
    """
    import base64
    from ..control_plane import WorkContract, Quota
    from ..workspace import Workspace
    from ..workspace.errors import WorkspaceError
    from .execution_responsibility import evidence_payloads, record_once, persisted_responsibility
    from ..verification.acceptance import fingerprint
    snapshot = workspace.manager.snapshot(lease.lease_id)
    if not snapshot.dirty_paths:
        return
    records = evidence_payloads(workspace.run_store, lease.run_id, 'EXECUTION_WORK_CONTRACT_V1')
    if len(records) != 1:
        raise ValueError('draft parent authority missing')
    raw = records[0]
    parent = WorkContract(**{**raw, 'quota': Quota(**raw['quota'])})
    bound = persisted_responsibility(workspace.run_store, lease.run_id)
    if bound is None or bound.work_contract_fingerprint != fingerprint(parent.to_dict()):
        raise ValueError('draft parent authority mismatch')
    for field in ('goal_id', 'todo_id', 'allowed_scope', 'forbidden_scope', 'constraints', 'required_evidence'):
        if getattr(parent, field) != getattr(contract, field):
            raise ValueError('draft authority scope or root constraints changed')
    session = workspace.factory.create(run=workspace.run_store.get_run(lease.run_id),
        todo=workspace.run_store.get_todo(lease.todo_id), contract=parent, root=lease.worktree_root)
    paths = Workspace(session)
    files = []
    size = 0
    if len(snapshot.dirty_paths) > 128 or snapshot.head_commit != lease.base_commit:
        raise ValueError('draft candidate base or size unsupported')
    for path in sorted(snapshot.dirty_paths):
        try:
            _, target = paths._candidate(path, mutation=True)
            # Reject even safe aliases: a transferred draft must be a regular,
            # exact path, not a recreated link with changed target authority.
            if any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400
                   for p in (target, *target.parents) if p != paths.root and paths.root in p.parents):
                raise ValueError('draft symlink/reparse point is not transferable')
            if not target.is_file():
                raise ValueError('draft deletion is not transferable')
            data = target.read_bytes()
        except WorkspaceError as exc:
            raise ValueError('draft path authority rejected') from exc
        size += len(data)
        if size > 4 * 1024 * 1024:
            raise ValueError('draft exceeds bounded byte limit')
        files.append({'path': path, 'sha256': hashlib.sha256(data).hexdigest(),
                      'content_base64': base64.b64encode(data).decode('ascii')})
    if workspace.manager.snapshot(lease.lease_id) != snapshot:
        raise ValueError('draft changed during capture')
    for row in files:
        if hashlib.sha256(paths._candidate(row['path'], mutation=True)[1].read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('draft bytes changed during capture')
    payload = {'schema_version': 1, 'status': 'DRAFT_UNVERIFIED',
        'parent_run_id': lease.run_id, 'parent_lease_id': lease.lease_id,
        'parent_responsibility': bound.to_dict(), 'responsibility': responsibility.to_dict(),
        'parent_contract': raw, 'source_repo_root': lease.source_repo_root,
        'source_git_common_dir': lease.source_git_common_dir, 'base_commit': lease.base_commit,
        'source_head_commit': _source_head_commit(workspace.manager, lease),
        'snapshot_diff_sha256': snapshot.diff_sha256, 'files': files}
    record_once(workspace.run_store, lease.run_id, lease.todo_id,
        'REPLAN_DRAFT:' + fingerprint(responsibility.to_dict()), payload)


def apply_replan_draft(workspace, lease, contract, responsibility):
    """Apply an exact sealed draft once to a pristine, independently owned lease."""
    import base64
    from ..workspace import Workspace
    from .execution_responsibility import evidence_payloads, record_once
    from ..verification.acceptance import fingerprint
    kind = 'REPLAN_DRAFT:' + fingerprint(responsibility.to_dict())
    records = [value for run in workspace.run_store.list_runs()
               if run.active_todo_id == contract.todo_id
               for value in evidence_payloads(workspace.run_store, run.run_id, kind)]
    if not records:
        return
    if any(value != records[0] for value in records):
        raise ValueError('conflicting draft parents')
    draft = records[0]
    if (draft['responsibility'] != responsibility.to_dict() or draft['status'] != 'DRAFT_UNVERIFIED'
            or draft['parent_lease_id'] == lease.lease_id
            or draft['source_repo_root'] != lease.source_repo_root
            or draft['source_git_common_dir'] != lease.source_git_common_dir
            or draft['base_commit'] != lease.base_commit
            or draft['source_head_commit'] != _source_head_commit(workspace.manager, lease)):
        raise ValueError('stale draft candidate identity')
    # Current authority must still match the parent. Model guidance is not a
    # grant of new mutable scope, even if a record was imported incorrectly.
    for field in ('goal_id', 'todo_id', 'allowed_scope', 'forbidden_scope', 'constraints', 'required_evidence'):
        if draft['parent_contract'][field] != contract.to_dict()[field]:
            raise ValueError('draft authority scope changed')
    binding = {'status': 'DRAFT_UNVERIFIED', 'parent_run_id': draft['parent_run_id'],
        'parent_lease_id': draft['parent_lease_id'], 'lease_id': lease.lease_id,
        'draft_fingerprint': fingerprint(draft), 'responsibility': responsibility.to_dict()}
    applied = evidence_payloads(workspace.run_store, lease.run_id, 'REPLAN_DRAFT_APPLIED_V1')
    if applied:
        if applied != [binding]:
            raise ValueError('draft application binding mismatch')
        return
    snapshot = workspace.manager.snapshot(lease.lease_id)
    if snapshot.dirty_paths or snapshot.head_commit != lease.base_commit:
        raise ValueError('draft destination conflict; candidate is not pristine')
    session = workspace.factory.create(run=workspace.run_store.get_run(lease.run_id),
        todo=workspace.run_store.get_todo(lease.todo_id), contract=contract, root=lease.worktree_root)
    paths = Workspace(session)
    writes = []
    for row in draft['files']:
        _, target = paths._candidate(row['path'], mutation=True)
        data = base64.b64decode(row['content_base64'], validate=True)
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('draft file fingerprint mismatch')
        writes.append((target, data))
    record_once(workspace.run_store, lease.run_id, lease.todo_id, 'REPLAN_DRAFT_STARTED_V1', binding)
    for target, data in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        paths._candidate(target.relative_to(paths.root).as_posix(), mutation=True)
        target.write_bytes(data)
    for target, data in writes:
        if target.read_bytes() != data:
            raise ValueError('draft destination write mismatch')
    record_once(workspace.run_store, lease.run_id, lease.todo_id, 'REPLAN_DRAFT_APPLIED_V1', binding)


@dataclass(frozen=True, slots=True)
class CandidateProgressCheckpointV1:
    schema_version: int
    run_id: str
    todo_id: str
    lease_id: str
    branch_name: str
    base_commit: str
    source_head_commit: str
    diff_sha256: str
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    last_event_sequence: int
    failure_kind: str
    verification_state: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for value in (self.run_id, self.todo_id, self.lease_id, self.branch_name, self.base_commit, self.source_head_commit, self.diff_sha256, self.failure_kind):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("checkpoint identity must be non-empty")
        if self.verification_state != "UNVERIFIED" or type(self.last_event_sequence) is not int or self.last_event_sequence < 0:
            raise ValueError("invalid unverified candidate checkpoint")
        for paths in (self.changed_paths, self.untracked_paths):
            if tuple(sorted(set(paths))) != paths or any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in paths):
                raise ValueError("checkpoint paths must be normalized and unique")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "run_id": self.run_id, "todo_id": self.todo_id,
                "lease_id": self.lease_id, "branch_name": self.branch_name, "base_commit": self.base_commit,
                "source_head_commit": self.source_head_commit,
                "diff_sha256": self.diff_sha256, "changed_paths": list(self.changed_paths),
                "untracked_paths": list(self.untracked_paths), "last_event_sequence": self.last_event_sequence,
                "failure_kind": self.failure_kind, "verification_state": self.verification_state}

    @classmethod
    def from_dict(cls, payload: object) -> "CandidateProgressCheckpointV1":
        if not isinstance(payload, Mapping):
            raise ValueError("checkpoint payload must be an object")
        expected = {"schema_version", "run_id", "todo_id", "lease_id", "branch_name", "base_commit", "source_head_commit", "diff_sha256", "changed_paths", "untracked_paths", "last_event_sequence", "failure_kind", "verification_state"}
        if set(payload) != expected:
            raise ValueError("checkpoint fields mismatch")
        paths = []
        for name in ("changed_paths", "untracked_paths"):
            value = payload[name]
            if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
                raise ValueError("checkpoint paths must be string lists")
            paths.append(tuple(value))
        if type(payload["schema_version"]) is not int or type(payload["last_event_sequence"]) is not int:
            raise ValueError("checkpoint numeric fields must be exact integers")
        values = {name: payload[name] for name in expected - {"changed_paths", "untracked_paths"}}
        return cls(changed_paths=paths[0], untracked_paths=paths[1], **values)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record_event(*, run_store, run_id: str, todo_id: str, evidence_type: str,
                  evidence_id: str, payload: dict[str, object], reference: str | None = None) -> None:
    """Observational telemetry only: it cannot alter Run/Todo verification status."""
    run_store.record_evidence(run_id, Evidence(
        evidence_id=evidence_id, todo_id=todo_id, evidence_type=evidence_type,
        source="CandidateContinuityV1", status=EvidenceStatus.PASSED,
        payload=payload, reference=reference,
    ))


def record_recovery_block(*, run_store, run_id: str, todo_id: str,
                          failure_kind: str, reason: str) -> None:
    _record_event(
        run_store=run_store, run_id=run_id, todo_id=todo_id,
        evidence_type="RECOVERY_WORKSPACE_BLOCKED",
        evidence_id=f"{run_id}:recovery-workspace-blocked:{failure_kind}",
        payload={"schema_version": 1, "failure_kind": failure_kind,
                 "disposition": "BLOCK", "reason": reason},
    )


def record_candidate_disposition(*, run_store, run_id: str, todo_id: str,
                                 checkpoint: CandidateProgressCheckpointV1,
                                 reused: bool, reason: str) -> None:
    kind = "CANDIDATE_REUSED" if reused else "CANDIDATE_REUSE_DENIED"
    _record_event(
        run_store=run_store, run_id=run_id, todo_id=todo_id, evidence_type=kind,
        evidence_id=f"{run_id}:candidate-disposition:{checkpoint.fingerprint}:{kind}",
        payload={"schema_version": 1, "checkpoint_fingerprint": checkpoint.fingerprint,
                 "lease_id": checkpoint.lease_id, "failure_kind": checkpoint.failure_kind,
                 "reason": reason, "reused": reused},
    )


def record_delta_summary(*, artifact_store, run_store, run_id: str, todo_id: str,
                         checkpoint: CandidateProgressCheckpointV1) -> None:
    """Durable bounded handoff facts; the candidate remains explicitly UNVERIFIED."""
    payload: dict[str, object] = {
        "schema_version": 1, "checkpoint_fingerprint": checkpoint.fingerprint,
        "lease_id": checkpoint.lease_id, "changed_paths": list(checkpoint.changed_paths[:32]),
        "base_commit": checkpoint.base_commit, "diff_sha256": checkpoint.diff_sha256,
        "previous_failure_kind": checkpoint.failure_kind, "verification_state": "UNVERIFIED",
    }
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact_id = f"candidate-delta:{str(payload['fingerprint'])[:32]}"
    artifact = Artifact.from_text(
        artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE, content=json.dumps(payload, sort_keys=True),
        media_type="application/json", source="CandidateDeltaSummaryV1",
        created_at="1970-01-01T00:00:00+00:00",
        metadata={"production_role": "CANDIDATE_DELTA_SUMMARY", "run_id": run_id,
                  "todo_id": todo_id, "lease_id": checkpoint.lease_id,
                  "fingerprint": payload["fingerprint"]},
    )
    if not artifact_store.has_artifact(artifact_id):
        artifact_store.create_artifact(artifact)
    _record_event(
        run_store=run_store, run_id=run_id, todo_id=todo_id,
        evidence_type="CANDIDATE_DELTA_SUMMARY",
        evidence_id=f"{run_id}:candidate-delta:{payload['fingerprint']}",
        payload=payload, reference=f"artifact://{artifact_id}",
    )


def _untracked_paths(manager, lease, paths: tuple[str, ...]) -> tuple[str, ...]:
    git = getattr(manager, "git", None)
    root = getattr(lease, "worktree_root", None)
    if git is None or not isinstance(root, str):
        return ()
    values = []
    for path in paths:
        if git.run(Path(root), "ls-files", "--error-unmatch", "--", path, check=False).returncode != 0:
            values.append(path)
    return tuple(sorted(values))


def _source_head_commit(manager, lease) -> str:
    git = getattr(manager, "git", None)
    source_root = getattr(lease, "source_repo_root", None)
    if git is not None and isinstance(source_root, str):
        return git.text(Path(source_root), "rev-parse", "HEAD")
    # Test-only adapters without Git expose no independent source identity.
    return lease.base_commit


def checkpoint_candidate(*, artifact_store, run_store, manager, run_id: str, todo_id: str, lease, failure_kind: str) -> CandidateProgressCheckpointV1 | None:
    snapshot = manager.snapshot(lease.lease_id)
    if not snapshot.dirty_paths:
        return None
    events = run_store.list_events(run_id)
    checkpoint = CandidateProgressCheckpointV1(
        1, run_id, todo_id, lease.lease_id, lease.branch_name, lease.base_commit,
        _source_head_commit(manager, lease), snapshot.diff_sha256, tuple(sorted(snapshot.dirty_paths)),
        _untracked_paths(manager, lease, tuple(sorted(snapshot.dirty_paths))),
        events[-1].sequence, failure_kind,
    )
    artifact = Artifact.from_text(
        artifact_id=f"candidate-progress:{checkpoint.fingerprint[:32]}",
        kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE,
        content=json.dumps(checkpoint.to_dict(), sort_keys=True), media_type="application/json",
        source="CandidateProgressCheckpointV1", created_at="1970-01-01T00:00:00+00:00",
        metadata={"production_role": "CANDIDATE_PROGRESS_CHECKPOINT", "run_id": run_id,
                  "todo_id": todo_id, "lease_id": lease.lease_id, "fingerprint": checkpoint.fingerprint},
    )
    if not artifact_store.has_artifact(artifact.artifact_id):
        artifact_store.create_artifact(artifact)
    _record_event(
        run_store=run_store, run_id=run_id, todo_id=todo_id,
        evidence_type="CANDIDATE_PROGRESS_CHECKPOINT",
        evidence_id=f"{run_id}:candidate-progress:{checkpoint.fingerprint}",
        payload=checkpoint.to_dict(), reference=f"artifact://{artifact.artifact_id}",
    )
    return checkpoint


def candidate_restart_authorized(*, artifact_store, run_store, manager, run, lease, contract) -> bool:
    """Only an exact RC-6 V2 checkpoint can authorize a cold retained candidate."""
    try:
        from .candidate_cold_restart import cold_restart_authorization
        return cold_restart_authorization(
            artifact_store=artifact_store, run_store=run_store, manager=manager,
            run=run, lease=lease, contract=contract,
        ).authorized
    except Exception:
        return False
def validate_reuse(*, manager, lease, checkpoint: CandidateProgressCheckpointV1, contract) -> tuple[bool, str]:
    try:
        if (checkpoint.todo_id != lease.todo_id or checkpoint.run_id != lease.run_id
                or checkpoint.lease_id != lease.lease_id or checkpoint.branch_name != lease.branch_name
                or checkpoint.base_commit != lease.base_commit):
            return False, "candidate identity mismatch"
        snapshot = manager.snapshot(lease.lease_id)
        if snapshot.head_commit != checkpoint.base_commit or snapshot.diff_sha256 != checkpoint.diff_sha256:
            return False, "candidate diff or base mismatch"
        git = getattr(manager, "git", None)
        source_root = getattr(lease, "source_repo_root", None)
        if git is not None and isinstance(source_root, str):
            if git.text(Path(source_root), "rev-parse", "HEAD") != checkpoint.source_head_commit:
                return False, "source target branch changed"
        forbidden = set(contract.forbidden_scope)
        allowed = tuple(contract.allowed_scope)
        def in_scope(path: str) -> bool:
            return not allowed or any(path == root or path.startswith(root.rstrip("/") + "/") for root in allowed)
        if any(any(path == root or path.startswith(root.rstrip("/") + "/") for root in forbidden) or path.startswith((".git/", "state/")) or not in_scope(path) for path in checkpoint.changed_paths):
            return False, "candidate scope violation"
        return True, "validated unverified candidate"
    except Exception as exc:
        return False, f"candidate validation error: {type(exc).__name__}"