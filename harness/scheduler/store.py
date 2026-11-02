'Durable cross-process scheduler coordination store for scheduler.'
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..judge.models import JudgeAction
from .dag import derived_readiness, validate_dag
from .errors import SchedulerConflictError, SchedulerStateError, SchedulerStoreError
from .models import (
    ClaimStatus,
    SchedulerClaim,
    SchedulerLease,
    SchedulerLeaseStatus,
    SchedulerTodoState,
    SchedulerTodoStatus,
    WorkingSetPrediction,
)

_SCHEMA_VERSION = 1


class _FileMutex:
    def __init__(self, path: Path, timeout_seconds: int) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self) -> "_FileMutex":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0"); self.handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close(); self.handle = None
                    raise SchedulerStoreError("scheduler store lock timeout")
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close(); self.handle = None


class FileSchedulerStore:
    """One atomically replaced scheduler projection protected by an OS file lock."""

    def __init__(self, root: str | Path, *, lock_timeout_seconds: int = 10) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".scheduler.lock"
        self.lock_timeout_seconds = lock_timeout_seconds
        if not self.state_path.exists():
            self._atomic_write(self._empty())
        self._read_raw()  # fail fast on corruption

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": _SCHEMA_VERSION, "sequence": 0, "todos": {}, "claims": {}, "leases": {}}

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        with _FileMutex(self.lock_path, self.lock_timeout_seconds):
            raw = self._read_raw()
            yield raw
            raw["sequence"] = int(raw.get("sequence", 0)) + 1
            self._atomic_write(raw)

    def _atomic_write(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False, newline="\n") as handle:
                tmp = handle.name
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _read_raw(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SchedulerStoreError(f"cannot read scheduler state: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise SchedulerStoreError("unsupported or malformed scheduler state")
        if any(not isinstance(raw.get(key), dict) for key in ("todos", "claims", "leases")):
            raise SchedulerStoreError("scheduler state collections are malformed")
        try:
            states = tuple(SchedulerTodoState.from_dict(value) for value in raw["todos"].values())
            claims = tuple(SchedulerClaim.from_dict(value) for value in raw["claims"].values())
            leases = tuple(SchedulerLease.from_dict(value) for value in raw["leases"].values())
        except Exception as exc:
            raise SchedulerStoreError(f"invalid durable scheduler model: {exc}") from exc
        if len({x.todo_id for x in states}) != len(states):
            raise SchedulerStoreError("duplicate durable todo identity")
        if len({x.claim_id for x in claims}) != len(claims) or len({x.lease_id for x in leases}) != len(leases):
            raise SchedulerStoreError("duplicate durable claim/lease identity")
        active_claims: dict[str, str] = {}
        for claim in claims:
            if claim.status is ClaimStatus.ACTIVE:
                if claim.todo_id in active_claims:
                    raise SchedulerStoreError("multiple active claims for one Todo")
                active_claims[claim.todo_id] = claim.claim_id
        active_resources: set[str] = set()
        for lease in leases:
            if lease.status is SchedulerLeaseStatus.ACTIVE:
                if lease.resource_key in active_resources:
                    raise SchedulerStoreError("multiple active scheduler leases for one resource")
                active_resources.add(lease.resource_key)
        for state in states:
            expected = active_claims.get(state.todo_id)
            if state.active_claim_id != expected:
                raise SchedulerStoreError("Todo active_claim_id disagrees with durable active claim")
        return raw

    @staticmethod
    def _states(raw: dict) -> dict[str, SchedulerTodoState]:
        return {key: SchedulerTodoState.from_dict(value) for key, value in raw["todos"].items()}

    @staticmethod
    def _claims(raw: dict) -> dict[str, SchedulerClaim]:
        return {key: SchedulerClaim.from_dict(value) for key, value in raw["claims"].items()}

    @staticmethod
    def _leases(raw: dict) -> dict[str, SchedulerLease]:
        return {key: SchedulerLease.from_dict(value) for key, value in raw["leases"].items()}

    def register(self, states: tuple[SchedulerTodoState, ...]) -> tuple[SchedulerTodoState, ...]:
        validate_dag(states)
        goal_id = states[0].goal_id
        with self._locked() as raw:
            existing = self._states(raw)
            incoming = {x.todo_id: x for x in states}
            existing_goal = {k: v for k, v in existing.items() if v.goal_id == goal_id}
            if existing_goal:
                if set(existing_goal) != set(incoming):
                    raise SchedulerConflictError("scheduler DAG already registered with different Todo identities")
                for todo_id, proposed in incoming.items():
                    current = existing_goal[todo_id]
                    immutable_current = (current.goal_id, current.run_id, current.objective, current.dependencies, current.priority)
                    immutable_proposed = (proposed.goal_id, proposed.run_id, proposed.objective, proposed.dependencies, proposed.priority)
                    if immutable_current != immutable_proposed:
                        raise SchedulerConflictError("scheduler DAG already registered with different immutable Todo specification")
                return tuple(sorted(existing_goal.values(), key=lambda x: (-x.priority, x.todo_id.casefold())))
            collisions = set(incoming) & set(existing)
            if collisions:
                raise SchedulerConflictError("scheduler todo_id collision across goals")
            for todo_id, state in incoming.items():
                raw["todos"][todo_id] = state.to_dict()
        self.refresh_readiness(goal_id)
        return self.list_todos(goal_id)

    def list_todos(self, goal_id: str | None = None) -> tuple[SchedulerTodoState, ...]:
        raw = self._read_raw(); states = self._states(raw).values()
        if goal_id is not None:
            states = (x for x in states if x.goal_id == goal_id)
        return tuple(sorted(states, key=lambda x: (-x.priority, x.todo_id.casefold())))

    def get_todo(self, todo_id: str) -> SchedulerTodoState:
        raw = self._read_raw()
        if todo_id not in raw["todos"]:
            raise SchedulerStateError(f"unknown scheduler Todo {todo_id}")
        return SchedulerTodoState.from_dict(raw["todos"][todo_id])

    def revise_replan_required(self, *, todo_id: str, objective: str, amendment_id: str) -> SchedulerTodoState:
        'Atomically apply a journaled objective revision after Judge REPLAN.\n\n        The amendment ledger makes a replay after a crash between scheduler\n        mutation and mission journal acknowledgement idempotent.\n        '
        if not isinstance(objective, str) or not objective.strip():
            raise SchedulerStateError("replanned objective must be non-empty")
        with self._locked() as raw:
            applied = raw.setdefault("applied_amendments", {})
            previous = applied.get(amendment_id)
            if previous is not None:
                if previous != {"todo_id": todo_id, "objective": objective}:
                    raise SchedulerConflictError("DAG amendment identity conflict")
                return SchedulerTodoState.from_dict(raw["todos"][todo_id])
            state = self._states(raw).get(todo_id)
            if state is None or state.status is not SchedulerTodoStatus.REPLAN_REQUIRED:
                raise SchedulerStateError("DAG revision requires REPLAN_REQUIRED Todo")
            if state.active_claim_id is not None:
                raise SchedulerStateError("DAG revision cannot overwrite an active claim")
            updated = replace(
                state, objective=objective, status=SchedulerTodoStatus.READY,
                attempt_count=0, next_action=JudgeAction.NONE,
                blocked_reason=None, version=state.version + 1,
            )
            raw["todos"][todo_id] = updated.to_dict()
            applied[amendment_id] = {"todo_id": todo_id, "objective": objective}
            raw.setdefault("responsibility_revisions", {})[todo_id] = {
                "amendment_id": amendment_id, "version": updated.version,
                "objective": objective,
            }
            return updated

    def responsibility_revision(self, todo_id: str) -> dict | None:
        """The amendment revision, independent of claim/heartbeat versions."""
        raw = self._read_raw()
        revision = raw.get("responsibility_revisions", {}).get(todo_id)
        if revision is not None:
            if (not isinstance(revision, dict)
                or set(revision) != {"amendment_id", "version", "objective"}
                or type(revision["version"]) is not int or revision["version"] < 1
                or type(revision["amendment_id"]) is not str or not revision["amendment_id"]
                or raw["todos"][todo_id]["version"] < revision["version"]):
                raise SchedulerStateError("malformed responsibility revision")
            if raw.get("applied_amendments", {}).get(revision["amendment_id"]) != {
                "todo_id": todo_id, "objective": revision["objective"],
            }:
                raise SchedulerStateError("responsibility revision ledger mismatch")
            return dict(revision)
        # Historical single-amendment journals did not retain application version.
        # The amendment itself is still an exact durable generation authority.
        matches = [(key, value) for key, value in raw.get("applied_amendments", {}).items()
                   if value["todo_id"] == todo_id]
        if len(matches) > 1:
            raise SchedulerStateError("historical responsibility revision is ambiguous")
        if matches:
            key, value = matches[0]
            return {"amendment_id": key, "version": None, "objective": value["objective"]}
        return None

    def has_applied_amendment(self, amendment_id: str) -> bool:
        return amendment_id in self._read_raw().get("applied_amendments", {})

    def mark_replan_required(self, *, todo_id: str, reason: str) -> SchedulerTodoState:
        """Recover a journaled replan after its scheduler claim expired."""
        with self._locked() as raw:
            state = self._states(raw).get(todo_id)
            if state is None:
                raise SchedulerStateError(f"unknown scheduler Todo {todo_id}")
            if state.status is SchedulerTodoStatus.REPLAN_REQUIRED:
                return state
            if state.status not in {SchedulerTodoStatus.READY, SchedulerTodoStatus.PENDING} or state.active_claim_id:
                raise SchedulerStateError("unclaimed Todo required for replan recovery")
            updated = replace(
                state, status=SchedulerTodoStatus.REPLAN_REQUIRED,
                next_action=JudgeAction.REPLAN,
                blocked_reason=reason, version=state.version + 1,
            )
            raw["todos"][todo_id] = updated.to_dict()
            return updated

    def refresh_readiness(self, goal_id: str) -> tuple[SchedulerTodoState, ...]:
        with self._locked() as raw:
            states = self._states(raw)
            goal_states = tuple(x for x in states.values() if x.goal_id == goal_id)
            validate_dag(goal_states)
            changed = True
            while changed:
                changed = False
                for state in sorted(goal_states, key=lambda x: x.todo_id.casefold()):
                    current = states[state.todo_id]
                    status, reason = derived_readiness(current, states)
                    if status != current.status or reason != current.blocked_reason:
                        updated = replace(current, status=status, blocked_reason=reason, version=current.version + 1)
                        states[state.todo_id] = updated; raw["todos"][state.todo_id] = updated.to_dict(); changed = True
                goal_states = tuple(states[x.todo_id] for x in goal_states)
            result = tuple(sorted(goal_states, key=lambda x: (-x.priority, x.todo_id.casefold())))
        return result

    def set_working_set(self, todo_id: str, prediction: WorkingSetPrediction) -> SchedulerTodoState:
        with self._locked() as raw:
            states = self._states(raw); state = states.get(todo_id)
            if state is None: raise SchedulerStateError(f"unknown scheduler Todo {todo_id}")
            if state.status not in {SchedulerTodoStatus.PENDING, SchedulerTodoStatus.READY}:
                raise SchedulerStateError("working set can only be changed before claim")
            updated = replace(state, working_set=prediction, version=state.version + 1)
            raw["todos"][todo_id] = updated.to_dict()
            return updated

    def active_claims(self, *, now: int | None = None) -> tuple[SchedulerClaim, ...]:
        claims = tuple(x for x in self._claims(self._read_raw()).values() if x.status is ClaimStatus.ACTIVE and (now is None or x.expires_at > now))
        return tuple(sorted(claims, key=lambda x: x.claim_id.casefold()))

    def get_claim(self, claim_id: str) -> SchedulerClaim:
        claims = self._claims(self._read_raw())
        if claim_id not in claims: raise SchedulerStateError(f"unknown scheduler claim {claim_id}")
        return claims[claim_id]

    def claim(self, todo_id: str, owner_id: str, *, now: int, ttl_seconds: int, max_attempts: int, max_active_claims: int) -> SchedulerClaim:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            states, claims = self._states(raw), self._claims(raw)
            state = states.get(todo_id)
            if state is None: raise SchedulerStateError(f"unknown scheduler Todo {todo_id}")
            if state.status is not SchedulerTodoStatus.READY:
                raise SchedulerConflictError(f"Todo {todo_id} is not READY")
            if state.active_claim_id is not None:
                raise SchedulerConflictError(f"Todo {todo_id} already has active claim")
            if state.attempt_count >= max_attempts:
                raise SchedulerStateError(f"Todo {todo_id} attempt quota exhausted")
            if sum(1 for x in claims.values() if x.status is ClaimStatus.ACTIVE and x.expires_at > now) >= max_active_claims:
                raise SchedulerConflictError("max_active_claims reached")
            generation = 1 + max((x.generation for x in claims.values() if x.todo_id == todo_id), default=0)
            claim = SchedulerClaim.create(todo_id=todo_id, owner_id=owner_id, generation=generation, now=now, ttl_seconds=ttl_seconds)
            raw["claims"][claim.claim_id] = claim.to_dict()
            updated = replace(state, status=SchedulerTodoStatus.CLAIMED, attempt_count=state.attempt_count + 1, active_claim_id=claim.claim_id, blocked_reason=None, version=state.version + 1)
            raw["todos"][todo_id] = updated.to_dict()
            return claim

    def renew_claim(self, claim_id: str, *, owner_id: str, now: int, ttl_seconds: int) -> SchedulerClaim:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            claims = self._claims(raw); claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE or claim.expires_at <= now:
                raise SchedulerStateError("claim is not active")
            if claim.owner_id != owner_id:
                raise SchedulerConflictError("claim owner mismatch")
            renewed = replace(claim, expires_at=now + ttl_seconds)
            raw["claims"][claim_id] = renewed.to_dict(); return renewed

    def renew_lease(self, lease_id: str, *, owner_id: str, now: int, ttl_seconds: int) -> SchedulerLease:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            leases = self._leases(raw); lease = leases.get(lease_id)
            if lease is None or lease.status is not SchedulerLeaseStatus.ACTIVE or lease.expires_at <= now:
                raise SchedulerStateError("scheduler lease is not active")
            if lease.owner_id != owner_id:
                raise SchedulerConflictError("scheduler lease owner mismatch")
            renewed = replace(lease, expires_at=now + ttl_seconds)
            raw["leases"][lease_id] = renewed.to_dict(); return renewed

    def start(self, claim_id: str, *, now: int) -> SchedulerTodoState:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            claims, states = self._claims(raw), self._states(raw)
            claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE or claim.expires_at <= now:
                raise SchedulerStateError("claim is not active")
            state = states[claim.todo_id]
            if state.status is not SchedulerTodoStatus.CLAIMED or state.active_claim_id != claim_id:
                raise SchedulerStateError("Todo is not CLAIMED by this claim")
            updated = replace(state, status=SchedulerTodoStatus.RUNNING, version=state.version + 1)
            raw["todos"][state.todo_id] = updated.to_dict(); return updated

    def wait_decision(self, claim_id: str, *, now: int) -> SchedulerTodoState:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            claims, states = self._claims(raw), self._states(raw)
            claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE or claim.expires_at <= now:
                raise SchedulerStateError("claim is not active")
            state = states[claim.todo_id]
            if state.status is not SchedulerTodoStatus.RUNNING or state.active_claim_id != claim_id:
                raise SchedulerStateError("Todo is not RUNNING by this claim")
            updated = replace(state, status=SchedulerTodoStatus.WAITING_DECISION, version=state.version + 1)
            raw["todos"][state.todo_id] = updated.to_dict(); return updated

    def acquire_lease(self, *, claim_id: str, resource_key: str, owner_id: str, now: int, ttl_seconds: int) -> SchedulerLease:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            claims, leases = self._claims(raw), self._leases(raw)
            claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE or claim.expires_at <= now:
                raise SchedulerStateError("resource lease requires active scheduler claim")
            if claim.owner_id != owner_id:
                raise SchedulerConflictError("resource lease owner must match claim owner")
            active = next((x for x in leases.values() if x.resource_key == resource_key and x.status is SchedulerLeaseStatus.ACTIVE and x.expires_at > now), None)
            if active is not None:
                if active.claim_id == claim_id:
                    return active
                raise SchedulerConflictError(f"resource already leased: {resource_key}")
            lease = SchedulerLease.create(resource_key=resource_key, claim_id=claim_id, owner_id=owner_id, now=now, ttl_seconds=ttl_seconds)
            raw["leases"][lease.lease_id] = lease.to_dict(); return lease

    def release_lease(self, lease_id: str, *, now: int) -> SchedulerLease:
        with self._locked() as raw:
            leases = self._leases(raw); lease = leases.get(lease_id)
            if lease is None: raise SchedulerStateError(f"unknown scheduler lease {lease_id}")
            if lease.status is not SchedulerLeaseStatus.ACTIVE: return lease
            released = replace(lease, status=SchedulerLeaseStatus.RELEASED, released_at=now)
            raw["leases"][lease_id] = released.to_dict(); return released

    def active_leases(self, *, now: int | None = None) -> tuple[SchedulerLease, ...]:
        leases = tuple(x for x in self._leases(self._read_raw()).values() if x.status is SchedulerLeaseStatus.ACTIVE and (now is None or x.expires_at > now))
        return tuple(sorted(leases, key=lambda x: (x.resource_key.casefold(), x.lease_id.casefold())))

    def attach_worktree(self, claim_id: str, worktree_lease_id: str, *, now: int) -> SchedulerTodoState:
        with self._locked() as raw:
            self._expire_raw(raw, now)
            claims, states = self._claims(raw), self._states(raw)
            claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE: raise SchedulerStateError("active claim required")
            state = states[claim.todo_id]
            if state.active_claim_id != claim_id: raise SchedulerStateError("claim/Todo mismatch")
            if state.worktree_lease_id not in (None, worktree_lease_id): raise SchedulerConflictError("Todo already bound to another worktree lease")
            updated = replace(state, worktree_lease_id=worktree_lease_id, version=state.version + 1)
            raw["todos"][state.todo_id] = updated.to_dict(); return updated

    def finish_claim(self, *, claim_id: str, now: int, status: SchedulerTodoStatus, last_judge_report_id: str, next_action, blocked_reason: str | None = None) -> SchedulerTodoState:
        if status in {SchedulerTodoStatus.CLAIMED, SchedulerTodoStatus.RUNNING, SchedulerTodoStatus.WAITING_DECISION}:
            raise SchedulerStateError("finish_claim requires non-active destination status")
        with self._locked() as raw:
            claims, states, leases = self._claims(raw), self._states(raw), self._leases(raw)
            claim = claims.get(claim_id)
            if claim is None or claim.status is not ClaimStatus.ACTIVE:
                raise SchedulerStateError("judge decision requires active claim")
            state = states[claim.todo_id]
            if state.status is not SchedulerTodoStatus.WAITING_DECISION or state.active_claim_id != claim_id:
                raise SchedulerStateError("Todo must be WAITING_DECISION for judge application")
            released = replace(claim, status=ClaimStatus.RELEASED, released_at=now)
            raw["claims"][claim_id] = released.to_dict()
            for lease_id, lease in leases.items():
                if lease.claim_id == claim_id and lease.status is SchedulerLeaseStatus.ACTIVE:
                    raw["leases"][lease_id] = replace(lease, status=SchedulerLeaseStatus.RELEASED, released_at=now).to_dict()
            updated = replace(
                state, status=status, active_claim_id=None, last_judge_report_id=last_judge_report_id,
                next_action=next_action, blocked_reason=blocked_reason, version=state.version + 1,
            )
            raw["todos"][state.todo_id] = updated.to_dict(); return updated

    def expire_stale(self, *, now: int) -> tuple[str, ...]:
        with self._locked() as raw:
            return self._expire_raw(raw, now)

    def _expire_raw(self, raw: dict, now: int) -> tuple[str, ...]:
        states, claims, leases = self._states(raw), self._claims(raw), self._leases(raw)
        expired_claim_ids: list[str] = []
        for claim_id, claim in claims.items():
            if claim.status is ClaimStatus.ACTIVE and claim.expires_at <= now:
                raw["claims"][claim_id] = replace(claim, status=ClaimStatus.EXPIRED, released_at=now).to_dict(); expired_claim_ids.append(claim_id)
                state = states[claim.todo_id]
                if state.active_claim_id == claim_id and not state.status.terminal:
                    updated = replace(state, status=SchedulerTodoStatus.PENDING, active_claim_id=None, blocked_reason="claim_expired", version=state.version + 1)
                    raw["todos"][state.todo_id] = updated.to_dict(); states[state.todo_id] = updated
        for lease_id, lease in leases.items():
            if lease.status is SchedulerLeaseStatus.ACTIVE and (lease.expires_at <= now or lease.claim_id in expired_claim_ids):
                raw["leases"][lease_id] = replace(lease, status=SchedulerLeaseStatus.EXPIRED, released_at=now).to_dict()
        return tuple(sorted(expired_claim_ids, key=str.casefold))
