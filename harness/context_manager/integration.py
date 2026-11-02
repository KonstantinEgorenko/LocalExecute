'Control Plane ↔ ArtifactStore integration for context management context assembly.'
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..control_plane import Evidence, EvidenceStatus, RunStatus, WorkContract
from ..observability import ContextBuildObserver
from ..control_plane.store import RunStore
from .builder import ContextBuilder
from .errors import ArtifactNotFoundError, ContextResolutionError
from .models import (
    Artifact,
    ArtifactKind,
    ArtifactOrigin,
    ArtifactRef,
    ContextBudget,
    ContextBundle,
    ContextRequest,
    JsonValue,
)
from .store import ArtifactStore

ARTIFACT_URI_PREFIX = "artifact://"


def artifact_uri(artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    return f"{ARTIFACT_URI_PREFIX}{artifact_id}"


def parse_artifact_uri(reference: object) -> str | None:
    if not isinstance(reference, str) or not reference.startswith(ARTIFACT_URI_PREFIX):
        return None
    artifact_id = reference[len(ARTIFACT_URI_PREFIX) :]
    return artifact_id if artifact_id.strip() else None


class ControlPlaneArtifactBridge:
    """Publish text content as an Artifact and reference it from Evidence.

    Content is persisted first. A later Control Plane failure may leave an
    unreferenced content-addressed artifact, which is safe; the inverse (a
    published evidence reference to missing content) is never created here.
    """

    def __init__(self, run_store: RunStore, artifact_store: ArtifactStore) -> None:
        if not isinstance(run_store, RunStore):
            raise ValueError("run_store must implement RunStore")
        if not isinstance(artifact_store, ArtifactStore):
            raise ValueError("artifact_store must implement ArtifactStore")
        self.run_store = run_store
        self.artifact_store = artifact_store

    def record_text_evidence(
        self,
        run_id: str,
        *,
        artifact_id: str,
        todo_id: str,
        evidence_id: str,
        evidence_type: str,
        source: str,
        content: str,
        kind: ArtifactKind,
        origin: ArtifactOrigin,
        media_type: str = "text/plain",
        status: EvidenceStatus = EvidenceStatus.PASSED,
        scope_tags: tuple[str, ...] = (),
        metadata: Mapping[str, JsonValue] | None = None,
        evidence_payload: Mapping[str, JsonValue] | None = None,
        created_at: str | None = None,
    ) -> Evidence:
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(todo_id)
        if todo.goal_id != run.goal_id or run.active_todo_id not in (None, todo_id):
            raise ContextResolutionError("evidence Todo must match the target Run")
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        artifact = Artifact.from_text(
            artifact_id=artifact_id,
            kind=kind,
            origin=origin,
            content=content,
            media_type=media_type,
            source=source,
            created_at=timestamp,
            scope_tags=scope_tags,
            metadata={} if metadata is None else metadata,
        )
        self.artifact_store.create_artifact(artifact)
        evidence = Evidence(
            evidence_id=evidence_id,
            todo_id=todo_id,
            evidence_type=evidence_type,
            source=source,
            status=status,
            payload={} if evidence_payload is None else evidence_payload,
            reference=artifact_uri(artifact_id),
        )
        self.run_store.record_evidence(run_id, evidence)
        return evidence


class ControlPlaneContextResolver:
    """Create a ContextRequest from one active Run/Todo and its dependencies."""

    def __init__(
        self,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        builder: ContextBuilder | None = None,
    ) -> None:
        if not isinstance(run_store, RunStore):
            raise ValueError("run_store must implement RunStore")
        if not isinstance(artifact_store, ArtifactStore):
            raise ValueError("artifact_store must implement ArtifactStore")
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.builder = builder or ContextBuilder(artifact_store)

    def assemble(
        self,
        run_id: str,
        contract: WorkContract,
        budget: ContextBudget,
        *,
        dependency_run_ids: Mapping[str, str] | None = None,
        required_artifacts: tuple[ArtifactRef, ...] = (),
        optional_artifacts: tuple[ArtifactRef, ...] = (),
        request_id: str | None = None,
        observer: ContextBuildObserver | None = None,
    ) -> ContextBundle:
        if not isinstance(contract, WorkContract):
            raise ValueError("contract must be a WorkContract")
        if not isinstance(budget, ContextBudget):
            raise ValueError("budget must be a ContextBudget")
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(contract.todo_id)
        if (
            run.goal_id != contract.goal_id
            or todo.goal_id != contract.goal_id
            or run.active_todo_id != contract.todo_id
        ):
            raise ContextResolutionError("WorkContract does not match active Run/Todo")

        mapping = dict(dependency_run_ids or {})
        unknown = set(mapping) - set(todo.dependencies)
        if unknown:
            raise ContextResolutionError(f"unknown dependency_run_ids: {sorted(unknown)}")
        missing = set(todo.dependencies) - set(mapping)
        if missing:
            raise ContextResolutionError(f"missing dependency_run_ids: {sorted(missing)}")

        explicit_required = tuple(required_artifacts)

        dependency_refs: list[ArtifactRef] = []
        for dependency_todo_id in sorted(todo.dependencies):
            dependency_run_id = mapping[dependency_todo_id]

            projected_ref = self._explicit_verified_dependency_ref(
                explicit_required,
                todo_id=dependency_todo_id,
                run_id=dependency_run_id,
                goal_id=contract.goal_id,
            )

            if projected_ref is not None:
                dependency_refs.append(projected_ref)
                continue

            dependency_refs.extend(
                self._dependency_refs(
                    dependency_todo_id,
                    dependency_run_id,
                    contract.goal_id,
                )
            )

        required_by_id = self._unique_refs(
            explicit_required + tuple(dependency_refs),
            "required",
        )
        optional_by_id = self._unique_refs(tuple(optional_artifacts), "optional")
        overlap = set(required_by_id) & set(optional_by_id)
        if overlap:
            raise ContextResolutionError(
                f"required and optional artifact identities overlap: {sorted(overlap)}"
            )

        request = ContextRequest(
            request_id=request_id or f"{run_id}:{contract.todo_id}:context",
            goal_id=contract.goal_id,
            todo_id=contract.todo_id,
            objective=todo.objective,
            work_contract=contract,
            required_artifacts=tuple(required_by_id[key] for key in sorted(required_by_id)),
            optional_artifacts=tuple(optional_by_id[key] for key in sorted(optional_by_id)),
            budget=budget,
        )
        dependency_ids = tuple(ref.artifact_id for ref in dependency_refs)
        return self.builder.build(request, dependency_artifact_ids=dependency_ids, observer=observer)

    def _explicit_verified_dependency_ref(
        self,
        required_artifacts: tuple[ArtifactRef, ...],
        *,
        todo_id: str,
        run_id: str,
        goal_id: str,
    ) -> ArtifactRef | None:
        'Resolve one explicit workflow verified dependency projection.\n\n        An explicitly supplied VERIFIED_TODO_OUTCOME is authoritative for the\n        matching dependency Run/Todo. Operational evidence from that Run must\n        not be re-expanded into the current Todo context.\n\n        Other required artifacts are ignored by this selector and remain\n        normal required context.\n        '
        matches: list[ArtifactRef] = []

        for ref in required_artifacts:
            try:
                artifact = self.artifact_store.get_artifact(
                    ref.artifact_id
                )
            except ArtifactNotFoundError:
                # ContextBuilder remains the authority for required-artifact
                # existence/integrity validation.
                continue

            metadata = artifact.metadata

            if metadata.get("phase16_role") != "VERIFIED_TODO_OUTCOME":
                continue

            artifact_todo_id = metadata.get("todo_id")

            # A verified outcome for another dependency is not a candidate
            # for this dependency.
            if artifact_todo_id != todo_id:
                continue

            if metadata.get("goal_id") != goal_id:
                raise ContextResolutionError(
                    "explicit verified dependency outcome goal identity mismatch: "
                    f"{todo_id}"
                )

            if metadata.get("run_id") != run_id:
                raise ContextResolutionError(
                    "explicit verified dependency outcome Run identity mismatch: "
                    f"{todo_id}"
                )

            if artifact.ref != ref:
                raise ContextResolutionError(
                    "explicit verified dependency outcome ArtifactRef mismatch: "
                    f"{ref.artifact_id}"
                )

            matches.append(ref)

        if len(matches) > 1:
            raise ContextResolutionError(
                "multiple explicit verified dependency outcomes: "
                f"{todo_id}"
            )

        return matches[0] if matches else None

    def _dependency_refs(self, todo_id: str, run_id: str, goal_id: str) -> tuple[ArtifactRef, ...]:
        dependency_todo = self.run_store.get_todo(todo_id)
        dependency_run = self.run_store.get_run(run_id)
        if (
            dependency_todo.goal_id != goal_id
            or dependency_run.goal_id != goal_id
            or dependency_run.active_todo_id != todo_id
        ):
            raise ContextResolutionError(f"dependency Run/Todo identity mismatch: {todo_id}")
        if dependency_run.status is not RunStatus.COMPLETED:
            raise ContextResolutionError(f"dependency run must be COMPLETED: {run_id}")

        refs: dict[str, ArtifactRef] = {}
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("status") != EvidenceStatus.PASSED.value:
                continue
            artifact_id = parse_artifact_uri(payload.get("reference"))
            if artifact_id is None:
                continue
            try:
                ref = self.artifact_store.get_ref(artifact_id)
            except ArtifactNotFoundError as exc:
                raise ContextResolutionError(
                    f"dependency evidence references missing artifact: {artifact_id}"
                ) from exc
            previous = refs.get(artifact_id)
            if previous is not None and previous != ref:
                raise ContextResolutionError(f"conflicting dependency artifact ref: {artifact_id}")
            refs[artifact_id] = ref
        if not refs:
            raise ContextResolutionError(
                f"dependency has no PASSED artifact-backed evidence: {todo_id}"
            )
        return tuple(refs[key] for key in sorted(refs))

    @staticmethod
    def _unique_refs(values: tuple[ArtifactRef, ...], label: str) -> dict[str, ArtifactRef]:
        refs: dict[str, ArtifactRef] = {}
        for ref in values:
            if not isinstance(ref, ArtifactRef):
                raise ContextResolutionError(f"{label}_artifacts must contain ArtifactRef values")
            previous = refs.get(ref.artifact_id)
            if previous is not None and previous != ref:
                raise ContextResolutionError(f"conflicting {label} ArtifactRef: {ref.artifact_id}")
            refs[ref.artifact_id] = ref
        return refs
