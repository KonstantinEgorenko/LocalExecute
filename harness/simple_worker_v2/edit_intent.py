"""Bounded discovery responsibility; source code and acceptance stay external."""
from dataclasses import asdict, dataclass
import json
import re

from harness.workspace import Workspace
from harness.workspace.workspace import normalize_relative_path
from harness.repo_intelligence.scanner import _kind
from harness.repo_intelligence.models import RepositoryFileKind
from .interaction_state import fingerprint, current_observation


class DiscoveryError(ValueError):
    def __init__(self, category, detail):
        self.category = category
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class TypedEditIntentV1:
    schema_version: int
    run_id: str
    todo_id: str
    responsibility_fingerprint: str
    objective: str
    source_versions: tuple[tuple[str, str, str], ...]
    test_versions: tuple[tuple[str, str, str], ...]
    relevant_facts: tuple[str, ...]
    verification_obligations: tuple[str, ...]

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('intent schema_version must be integer 1')
        if any(type(getattr(self, f)) is not str or not getattr(self, f).strip()
               for f in ('run_id', 'todo_id', 'responsibility_fingerprint', 'objective')):
            raise ValueError('intent identity/objective must be nonempty strings')
        for name in ('source_versions', 'test_versions'):
            rows = getattr(self, name)
            if type(rows) is not tuple or len(rows) > 4:
                raise ValueError('intent versions must be bounded immutable tuples')
            for row in rows:
                if type(row) is not tuple or len(row) != 3:
                    raise ValueError('version binds logical path, canonical path and content SHA')
                path, canonical, version = row
                if (normalize_relative_path(path) != path or normalize_relative_path(canonical) != canonical
                        or not re.fullmatch('[0-9a-f]{64}', version)):
                    raise ValueError('intent path/version invalid')
        paths = [r[0] for r in (*self.source_versions, *self.test_versions)]
        if len(set(paths)) != len(paths):
            raise ValueError('intent targets must be distinct')
        for name in ('relevant_facts', 'verification_obligations'):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > 8 or any(
                    type(s) is not str or not s.strip() or len(s.encode('utf-8')) > 512 for s in values):
                raise ValueError('intent facts/obligations must be bounded strings')
        if len(json.dumps(self.to_dict(), ensure_ascii=False).encode('utf-8')) > 8192:
            raise ValueError('intent exceeds 8 KiB')

    @property
    def source_targets(self):
        return tuple(row[0] for row in self.source_versions)

    @property
    def test_targets(self):
        return tuple(row[0] for row in self.test_versions)

    def to_dict(self):
        return asdict(self)

    def validate_current(self, session, contract):
        if (self.run_id != session.run_id or self.todo_id != contract.todo_id
                or self.responsibility_fingerprint != fingerprint(json.dumps(
                    contract.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False))):
            raise DiscoveryError('INTENT_STALE', 'intent responsibility identity changed')
        workspace = Workspace(session)
        for path, canonical, version in (*self.source_versions, *self.test_versions):
            _, target = workspace._candidate(path, mutation=True)
            actual = target.resolve(strict=True).relative_to(workspace.root).as_posix()
            _, sha, _ = current_observation(workspace, path, 'FILE')
            if (actual, sha) != (canonical, version):
                raise DiscoveryError('INTENT_STALE', 'observed target version changed')


def resolve_edit_intent(*, contract, session, state, source_targets, test_targets, relevant_facts=()):
    """The model selects responsibility; Workspace/observations confer authority."""
    if len(source_targets) != 1 or len(test_targets) > 1:
        raise DiscoveryError('STANDARD_REQUIRED', 'native SIMPLE supports one source and one test target')
    if contract.constraints.get('REQUIRES_REGRESSION_CHANGE', False) and len(test_targets) != 1:
        raise DiscoveryError('DISCOVERY_INCOMPLETE', 'verified regression target required')
    if not test_targets:
        raise DiscoveryError('STANDARD_REQUIRED', 'qualified native split requires public test context')
    state = state.validated(session, run_id=session.run_id)
    current = {o.path: o for o in state.observations if o.action == 'FILE' and o.fresh and o.output}
    versions = []
    for paths, expected in ((source_targets, RepositoryFileKind.SOURCE), (test_targets, RepositoryFileKind.TEST)):
        rows = []
        for path in paths:
            if type(path) is not str or path not in current:
                raise DiscoveryError('DISCOVERY_INCOMPLETE', 'target requires a fresh successful file VIEW')
            if _kind(path) is not expected:
                raise DiscoveryError('DISCOVERY_INCOMPLETE', 'target responsibility does not match repository kind')
            item = current[path]
            workspace = Workspace(session)
            _, target = workspace._candidate(path, mutation=True)
            canonical = target.resolve(strict=True).relative_to(workspace.root).as_posix()
            workspace._candidate(canonical, mutation=True)
            if _kind(canonical) is not expected:
                raise DiscoveryError('DISCOVERY_INCOMPLETE', 'canonical target responsibility mismatch')
            rows.append((path, canonical, item.version))
        versions.append(tuple(rows))
    intent = TypedEditIntentV1(1, session.run_id, contract.todo_id, state.responsibility_fingerprint,
        contract.objective, *versions, tuple(relevant_facts), tuple(contract.required_evidence))
    intent.validate_current(session, contract)
    return intent
