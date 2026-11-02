"""Bounded verified ACI knowledge, persisted by the existing RunStore adapter.

No inference replay, candidate acceptance, or model reasoning belongs here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import posixpath

from harness.repo_intelligence.models import RepositoryFileKind
from harness.repo_intelligence.scanner import _kind
from harness.workspace import Workspace
from harness.workspace.errors import WorkspaceError
from harness.control_plane import Evidence, EvidenceStatus


PROJECTION_BYTES = 8192
STATE_BYTES = 24576


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def normalized(path):
    value = posixpath.normpath(path.replace('\\', '/'))
    return '' if value == '.' else value


def restore_interaction_state(store, *, run_id, todo_id, contract, session):
    responsibility = fingerprint(encoded(contract.to_dict()))
    candidates = []
    for run in store.list_runs():
        if run.active_todo_id != todo_id or run.goal_id != contract.goal_id:
            continue
        for event in store.list_events(run.run_id):
            raw = event.to_dict()
            evidence = raw['payload']
            if evidence.get('evidence_type') == 'INTERACTION_STATE_V1' and evidence.get('todo_id') == todo_id:
                state = InteractionStateV1.from_dict(evidence['payload']['state'])
                if state.todo_id != todo_id or state.run_id != run.run_id:
                    raise ValueError('persisted interaction identity mismatch')
                if state.responsibility_fingerprint == responsibility:
                    candidates.append((raw['occurred_at'], raw['sequence'], state))
    if candidates:
        state = max(candidates, key=lambda item: item[:2])[2]
        return state.validated(session, run_id=run_id)
    return InteractionStateV1(run_id, todo_id, responsibility)


def persist_state(store, state):
    store.record_evidence(state.run_id, Evidence(
        f'{state.run_id}:interaction:{len(store.list_events(state.run_id))}', state.todo_id,
        'INTERACTION_STATE_V1', 'WorkspaceAciV2', EvidenceStatus.PASSED,
        {'state': state.to_dict(), 'authority': 'verified observations only; not acceptance'}))


@dataclass(frozen=True, slots=True)
class KnownObservationV1:
    action: str
    path: str
    canonical_path: str
    version: str
    output: str
    observation_key: str
    fresh: bool = True

    def __post_init__(self):
        if self.action not in ('FILE', 'DIRECTORY', 'SEARCH'):
            raise ValueError('invalid observation action')
        if any(type(getattr(self, f)) is not str for f in
               ('path', 'canonical_path', 'version', 'output', 'observation_key')):
            raise ValueError('observation fields must be strings')
        if type(self.fresh) is not bool or len(self.output.encode('utf-8')) > 8192:
            raise ValueError('invalid bounded observation')
        if not self.fresh and self.output:
            raise ValueError('stale observations cannot contain source bytes')


def current_observation(workspace, path, action):
    """Authorize both paths again. Never read around Workspace policy."""
    _, target = workspace._candidate(path, allow_root=action == 'DIRECTORY')
    canonical = target.relative_to(workspace.root).as_posix()
    if action == 'FILE':
        _, text = workspace.read_text(path)
    else:
        text = encoded(workspace.list_files(path, recursive=False))
    return canonical, fingerprint(text), text


@dataclass(frozen=True, slots=True)
class InteractionStateV1:
    run_id: str
    todo_id: str
    responsibility_fingerprint: str
    execution_epoch: int = 0
    state_version: int = 0
    observations: tuple[KnownObservationV1, ...] = ()
    relevant_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    last_typed_failures: tuple[str, ...] = ()
    failed_edit_fingerprints: tuple[tuple[str, str, str], ...] = ()
    last_verification_summary: str | None = None
    no_progress_count: int = 0

    def __post_init__(self):
        for field in ('run_id', 'todo_id', 'responsibility_fingerprint'):
            if type(getattr(self, field)) is not str or not getattr(self, field):
                raise ValueError('interaction identity must be explicit')
        for field in ('execution_epoch', 'state_version', 'no_progress_count'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 0:
                raise ValueError('interaction counters must be nonnegative integers')
        for field in ('observations', 'relevant_paths', 'changed_paths',
                      'last_typed_failures', 'failed_edit_fingerprints'):
            if type(getattr(self, field)) is not tuple:
                raise ValueError('interaction collections must be immutable tuples')
        if len(self.observations) > 24 or len(self.relevant_paths) > 64:
            raise ValueError('interaction collection bound exceeded')
        if any(not isinstance(x, KnownObservationV1) for x in self.observations):
            raise ValueError('observations must be typed')
        if any(type(x) is not str for field in ('relevant_paths', 'changed_paths', 'last_typed_failures')
               for x in getattr(self, field)):
            raise ValueError('interaction facts must be strings')
        if any(type(x) is not tuple or len(x) != 3 or any(type(v) is not str for v in x)
               for x in self.failed_edit_fingerprints):
            raise ValueError('failed edits must bind path, version and proposal')
        if self.last_verification_summary is not None and (
                type(self.last_verification_summary) is not str or
                len(self.last_verification_summary.encode('utf-8')) > 2048):
            raise ValueError('verification summary must be bounded')
        if len(encoded(self.to_dict()).encode('utf-8')) > STATE_BYTES:
            raise ValueError('interaction state exceeds durable bound')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value['observations'] = tuple(KnownObservationV1(**v) for v in value['observations'])
        for field in ('relevant_paths', 'changed_paths', 'last_typed_failures'):
            value[field] = tuple(value[field])
        value['failed_edit_fingerprints'] = tuple(tuple(x) for x in value['failed_edit_fingerprints'])
        return cls(**value)

    def validated(self, session, *, run_id):
        workspace = Workspace(session)
        observations = []
        relevant = []
        changed_evidence = False
        for path in self.relevant_paths:
            try:
                workspace._candidate(path, allow_root=True)
                relevant.append(path)
            except (OSError, ValueError, WorkspaceError):
                pass
        for item in self.observations:
            # Search result-set completeness is never asserted after a boundary.
            # Authorized locations survive as stable knowledge; snippets do not.
            try:
                if item.action == 'SEARCH':
                    continue
                canonical, version, _ = current_observation(workspace, item.path, item.action)
            except (OSError, ValueError, WorkspaceError):
                changed_evidence = changed_evidence or item.fresh
                continue
            fresh = canonical == item.canonical_path and version == item.version
            changed_evidence = changed_evidence or (item.fresh and not fresh)
            observations.append(replace(item, fresh=fresh, output=item.output if fresh else ''))
        versions = {o.path: o.version for o in observations if o.fresh and o.action == 'FILE'}
        edits = tuple(e for e in self.failed_edit_fingerprints if versions.get(e[0]) == e[1])
        return replace(self, run_id=run_id, execution_epoch=self.execution_epoch + (run_id != self.run_id),
                       observations=tuple(observations), relevant_paths=tuple(relevant),
                       changed_paths=(), failed_edit_fingerprints=edits,
                       no_progress_count=0 if changed_evidence else self.no_progress_count)

    def viewed_files(self):
        result = {}
        for item in self.observations:
            if item.action == 'FILE' and item.fresh and item.output and len(item.output.splitlines()) <= 128:
                result[item.path] = item.output
                while len(result) > 4 or sum(len(t.encode('utf-8')) for t in result.values()) > 8192:
                    result.pop(next(iter(result)))
        return result

    def with_verification(self, summary):
        """A new authoritative report is new evidence, not repeated inspection."""
        return replace(self, last_verification_summary=summary,
                       no_progress_count=0 if summary != self.last_verification_summary else self.no_progress_count)

    def observe(self, session, action, observation, *, edit_key=None):
        workspace = Workspace(session)
        path = normalized(action.path)
        items = list(self.observations)
        relevant = list(self.relevant_paths)
        failures = list(self.last_typed_failures)
        edits = list(self.failed_edit_fingerprints)
        repeated = False
        new_fact = False
        if observation.ok and action.kind.value == 'VIEW':
            kind = 'DIRECTORY' if 'entry_count' in observation.metadata else 'FILE'
            try:
                canonical, version, text = current_observation(workspace, path, kind)
                # Bind only the bytes actually returned by the successful ACI.
                if kind == 'FILE' and text != observation.output:
                    raise ValueError('observation changed before binding')
                if kind == 'DIRECTORY' and text != encoded(json.loads(observation.output)):
                    raise ValueError('directory changed before binding')
                key = fingerprint(encoded([kind, path, canonical, version]))
                repeated = any(o.observation_key == key and o.fresh for o in items)
                new_fact = not repeated
                items = [o for o in items if not (o.path == path and o.action == kind)]
                material = text if len(text.encode('utf-8')) <= 8192 else ''
                items.append(KnownObservationV1(kind, path, canonical, version, material, key))
                if path not in relevant:
                    relevant.append(path)
                if kind == 'DIRECTORY':
                    for row in json.loads(observation.output):
                        if row['path'] not in relevant:
                            relevant.append(row['path'])
            except (OSError, ValueError, WorkspaceError):
                items = [o for o in items if o.path != path]
        elif observation.ok and action.kind.value == 'SEARCH':
            for row in observation.metadata.get('match_locations', ())[:32]:
                if row['path'] not in relevant:
                    relevant.append(row['path'])
            key = fingerprint(encoded([path, action.query, action.case_sensitive, observation.output]))
            repeated = any(o.observation_key == key and o.fresh for o in items)
            new_fact = not repeated
            items = [o for o in items if o.observation_key != key]
            items.append(KnownObservationV1('SEARCH', path, '', '', '', key))
        if not observation.ok:
            # No rejected candidate source, raw action or reasoning is cached.
            failure = encoded({'action': action.kind.value, 'path': path,
                               'error_code': observation.error_code.value,
                               'detail': observation.output.encode('utf-8')[:512].decode('utf-8', errors='ignore')})
            failures = (failures + [failure])[-4:]
            if observation.error_code.value == 'LOW_LEVEL_CONFLICT':
                items = [replace(o, fresh=False, output='') if o.path == path else o for o in items]
                edits = [e for e in edits if e[0] != path]
            if edit_key is not None and observation.error_code.value == 'NO_EFFECT':
                try:
                    _, version, _ = current_observation(workspace, path, 'FILE')
                    edits = (edits + [(path, version, edit_key[1])])[-16:]
                except (OSError, ValueError, WorkspaceError):
                    pass
        if observation.changed_paths or action.kind.value == 'RUN':
            dirty = set(observation.changed_paths)
            items = [replace(o, fresh=False, output='') if action.kind.value == 'RUN' or
                     o.path in dirty or o.action in ('DIRECTORY', 'SEARCH') else o for o in items]
            edits = [e for e in edits if e[0] not in dirty] if action.kind.value != 'RUN' else []
            new_fact = True
        items = items[-24:]
        # Evict old material observations first; hard durable and projection caps.
        while sum(len(encoded(asdict(o)).encode('utf-8')) for o in items) > 12000:
            items.pop(0)
        relevant = relevant[-64:]
        while len(encoded(relevant).encode('utf-8')) > 4096:
            relevant.pop(0)
        changed = list(dict.fromkeys((*self.changed_paths, *observation.changed_paths)))[-64:]
        while len(encoded(changed).encode('utf-8')) > 4096:
            changed.pop(0)
        updates = dict(state_version=self.state_version + 1, observations=tuple(items),
                        relevant_paths=tuple(relevant), last_typed_failures=tuple(failures),
                        changed_paths=tuple(changed),
                        failed_edit_fingerprints=tuple(edits),
                        no_progress_count=0 if new_fact else self.no_progress_count + int(repeated))
        # Account for all fields together, including long authorized paths.
        payload = self.to_dict()
        payload.update(updates)
        payload['observations'] = [asdict(o) for o in items]
        for field in ('observations', 'failed_edit_fingerprints', 'relevant_paths', 'last_typed_failures', 'changed_paths'):
            values = list(payload[field])
            while len(encoded(payload).encode('utf-8')) > STATE_BYTES and values:
                values.pop(0)
                payload[field] = values
            updates[field] = tuple(KnownObservationV1(**o) for o in values) if field == 'observations' else tuple(values)
        state = replace(self, **updates)
        return state, repeated

    def projection(self, *, current_dirty_paths=(), remaining=()):
        facts = [{'action': o.action, 'path': o.path, 'version': o.version,
                  'fresh': o.fresh, 'materialized': bool(o.output),
                  'completed_intent': 'inspection'} for o in reversed(self.observations)]
        value = {'schema_version': 1, 'run_id': self.run_id, 'todo_id': self.todo_id,
                 'execution_epoch': self.execution_epoch, 'state_version': self.state_version,
                 'changed_paths': list(current_dirty_paths), 'last_typed_failures':
                 [json.loads(x) for x in self.last_typed_failures],
                 'last_verification_summary': self.last_verification_summary,
                 'relevant_paths': list(self.relevant_paths),
                 'known_source_files': [p for p in self.relevant_paths if _kind(p) is RepositoryFileKind.SOURCE],
                 'known_test_files': [p for p in self.relevant_paths if _kind(p) is RepositoryFileKind.TEST],
                 'required_remaining_obligations': list(remaining), 'observations': facts,
                 'no_progress_count': self.no_progress_count, 'acceptance_authority': 'fresh Phase7 and Judge only',
                 'last_typed_failures_are_historical': True,
                 'knowledge_is_bounded': True,
                 'content': 'Fresh bounded file bytes appear in UNTRUSTED_SOURCE_LINE_INDEX; stale bytes are absent.',
                 'view_policy': 'VIEW remains allowed for legitimate reinspection; unchanged repeated observations add no information.'}
        while len(encoded(value).encode('utf-8')) > PROJECTION_BYTES and facts:
            facts.pop()
        for field in ('relevant_paths', 'known_source_files', 'known_test_files', 'last_typed_failures', 'changed_paths'):
            while len(encoded(value).encode('utf-8')) > PROJECTION_BYTES and value[field]:
                value[field].pop(0)
                value['projection_truncated'] = True
        assert len(encoded(value).encode('utf-8')) <= PROJECTION_BYTES
        return encoded(value)
