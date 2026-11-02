"""Bounded diagnosis path metadata projected through existing Workspace authority."""
from dataclasses import dataclass
import json

from harness.workspace import Workspace
from harness.workspace.errors import WorkspaceError
from harness.workspace.workspace import normalize_relative_path, _prefix_match


@dataclass(frozen=True, slots=True)
class DiagnosisPathDomainV1:
    mutable_workspace_paths: tuple[str, ...]
    immutable_paths: tuple[str, ...]
    observed_relevant_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]

    def __post_init__(self):
        for field in self.__dataclass_fields__:
            values = getattr(self, field)
            if not isinstance(values, tuple) or len(values) > 16:
                raise ValueError('diagnosis path lists must be bounded tuples')
            try:
                canonical = tuple(normalize_relative_path(p) for p in values)
            except WorkspaceError as exc:
                raise ValueError('diagnosis paths must be safe repository-relative paths') from exc
            if canonical != values or len(set(values)) != len(values):
                raise ValueError('diagnosis paths must be canonical relative unique paths')
        if any(_prefix_match(p, prefix) for p in self.mutable_workspace_paths for prefix in self.immutable_paths):
            raise ValueError('immutable paths cannot authorize mutation')
        if set(self.changed_paths) - set(self.observed_relevant_paths):
            raise ValueError('changed paths must be authorized relevant paths')
        if len(json.dumps(self.to_dict()).encode('utf-8')) > 8192:
            raise ValueError('diagnosis path domain exceeds byte limit')

    @property
    def workspace_paths(self):
        return tuple(dict.fromkeys((*self.mutable_workspace_paths, *self.observed_relevant_paths)))

    def to_dict(self):
        return {name: list(getattr(self, name)) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value):
        if set(value) != set(cls.__dataclass_fields__):
            raise ValueError('diagnosis domain keys must match exactly')
        if any(not isinstance(paths, (list, tuple)) for paths in value.values()):
            raise ValueError('diagnosis path lists must be arrays')
        return cls(**{name: tuple(paths) for name, paths in value.items()})


def project_diagnosis_paths(*, session, contract, interaction=None, changed_paths=(), observed_paths=()):
    """No repository enumeration, guesses, source contents or new permission rules."""
    workspace = Workspace(session)
    observed, mutable, changed = [], [], []
    candidates = tuple(dict.fromkeys((*changed_paths, *contract.allowed_scope, *observed_paths,
                                     *(interaction.relevant_paths if interaction is not None else ()),
                                     *((o.path for o in interaction.observations) if interaction is not None else ()))))
    for path in candidates:
        try:
            _, target = workspace._candidate(path)
            if not target.is_file():
                continue
            canonical = target.resolve(strict=True).relative_to(workspace.root).as_posix()
            workspace._candidate(canonical)
            if canonical not in observed:
                if len(observed) == 16:
                    continue
                observed.append(canonical)
            if path in changed_paths and canonical not in changed:
                changed.append(canonical)
            try:
                workspace._candidate(path, mutation=True)
                workspace._candidate(canonical, mutation=True)
            except (OSError, ValueError, WorkspaceError):
                continue
            if canonical not in mutable:
                mutable.append(canonical)
        except (OSError, ValueError, WorkspaceError):
            continue
    immutable = tuple(dict.fromkeys(normalize_relative_path(p) for p in
        (*session.policy.forbidden_paths, *session.policy.protected_paths)))
    return DiagnosisPathDomainV1(tuple(mutable), immutable, tuple(observed), tuple(changed))
