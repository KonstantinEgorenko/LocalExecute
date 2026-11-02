'Artifact-backed external context spine for long-horizon workflows.\n\nThe spine is deliberately *not* workflow state. Control Plane / Scheduler remain\nworkflow authorities. The spine stores immutable context objects in the frozen\ncontext management ArtifactStore and projects a bounded, model-specific view per attempt.\n'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..context_manager import (
    Artifact,
    ArtifactKind,
    ArtifactOrigin,
    ArtifactRef,
    ContextBudget,
    ContextBundle,
    ContextItem,
    ContextSource,
)
from ..context_manager.errors import ArtifactIdentityError, ContextBudgetError, ContextResolutionError
from ..context_manager.store import ArtifactStore
from ..context_manager.token_estimator import Utf8TokenEstimator
from ..control_plane import WorkContract
from .models import (
    ContextPackV1,
    WorkflowExecutionPolicy,
    ProjectSnapshotV1,
    VerifiedTodoOutcomeV1,
    canonical_json,
    stable_sha256,
)


_ROOT_SOURCE = "ROOT_TASK_SOURCE"
_ROOT_CAPSULE = "ROOT_GOAL_CAPSULE"
_PROJECT_SNAPSHOT = "PROJECT_SNAPSHOT"
_VERIFIED_TODO_OUTCOME = "VERIFIED_TODO_OUTCOME"
_RECOVERY_CONTEXT = "RECOVERY_CONTEXT"


def _id_fragment(value: str) -> str:
    return stable_sha256(value)[:20]


def _artifact_role(artifact: Artifact) -> str | None:
    value = artifact.metadata.get("phase16_role")
    return value if isinstance(value, str) else None


def _metadata_text(artifact: Artifact, key: str) -> str | None:
    value = artifact.metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_int(artifact: Artifact, key: str) -> int | None:
    value = artifact.metadata.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class RootContextRefs:
    source: ArtifactRef
    capsule: ArtifactRef


class ExternalContextSpine:
    """Durable external memory + deterministic small-context projection.

    Invariants:
    - full root task is retained externally but not injected into every worker;
    - a compact root capsule is pinned into every worker context;
    - latest verified project snapshot is pinned;
    - compact VERIFIED_TODO_OUTCOME artifacts are pinned for dependencies;
    - repository files are optional, ranked and bounded;
    - required context is fail-closed if it cannot fit a profile budget.
    """

    def __init__(self, artifact_store: ArtifactStore, policy: WorkflowExecutionPolicy) -> None:
        if not isinstance(artifact_store, ArtifactStore):
            raise ValueError("artifact_store must implement ArtifactStore")
        if not isinstance(policy, WorkflowExecutionPolicy):
            raise ValueError("policy must be WorkflowExecutionPolicy")
        self.artifact_store = artifact_store
        self.policy = policy
        self.token_estimator = Utf8TokenEstimator()

    def install_dependency_handoff(self, resolver, observer):
        """Production integration supplies exact dependency refs; legacy default is unchanged."""
        self.dependency_resolver = resolver
        self.dependency_context_observer = observer

    def dependency_context(self, goal_id, todo_id):
        return (self.dependency_resolver(goal_id,todo_id)
                if callable(getattr(self,'dependency_resolver',None)) else None)

    def required_root_ref(self, *, goal_id, todo_id, objective, contract, dependency_refs, profile_id):
        from .root_projection import VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1 as key
        authority = getattr(self, 'root_projection_authority', None)
        if key not in contract.constraints:
            if authority is not None and goal_id in authority.admitted_missions:
                raise ContextResolutionError('ROOT_PROJECTION_INVALID: admitted policy removed from contract')
            return self.root_context(goal_id).capsule
        if contract.constraints[key] is not True or authority is None:
            raise ContextResolutionError('ROOT_PROJECTION_INVALID: trusted policy binding required')
        return authority.project(goal_id=goal_id, todo_id=todo_id, objective=objective,
                                 contract=contract, dependency_refs=dependency_refs, profile_id=profile_id)

    def _ensure(self, artifact: Artifact) -> ArtifactRef:
        if self.artifact_store.has_artifact(artifact.artifact_id):
            existing = self.artifact_store.get_artifact(artifact.artifact_id)
            if existing.sha256 != artifact.sha256 or existing.content != artifact.content or existing.metadata != artifact.metadata:
                raise ArtifactIdentityError(f"Phase 16 artifact identity conflict: {artifact.artifact_id}")
            return existing.ref
        return self.artifact_store.create_artifact(artifact).ref

    def publish_root_context(
        self,
        *,
        goal_id: str,
        raw_objective: str,
        goal_capsule: str,
        global_constraints: Mapping[str, object] | None = None,
    ) -> RootContextRefs:
        constraints = dict(global_constraints or {})
        source_payload = {
            "schema_version": 1,
            "goal_id": goal_id,
            "objective": raw_objective,
            "constraints": constraints,
        }
        source = Artifact.from_text(
            artifact_id=f"phase16:root-source:{_id_fragment(goal_id)}",
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.USER,
            content=canonical_json(source_payload),
            media_type="application/json",
            source="Phase16RootTaskSource",
            created_at="1970-01-01T00:00:00+00:00",
            metadata={
                "phase16_role": _ROOT_SOURCE,
                "goal_id": goal_id,
                "objective_sha256": stable_sha256(raw_objective),
            },
        )
        capsule_payload = {
            "schema_version": 1,
            "goal_id": goal_id,
            "goal_capsule": goal_capsule,
            "global_constraints": constraints,
            "root_source_artifact_id": source.artifact_id,
            "root_source_sha256": source.sha256,
        }
        capsule = Artifact.from_text(
            artifact_id=f"phase16:root-capsule:{_id_fragment(goal_id)}",
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=canonical_json(capsule_payload),
            media_type="application/json",
            source="Phase16RootGoalCapsule",
            created_at="1970-01-01T00:00:00+00:00",
            metadata={
                "phase16_role": _ROOT_CAPSULE,
                "goal_id": goal_id,
                "root_source_artifact_id": source.artifact_id,
            },
        )
        source_ref = self._ensure(source)
        capsule_ref = self._ensure(capsule)
        return RootContextRefs(source_ref, capsule_ref)

    def publish_project_snapshot(self, snapshot: ProjectSnapshotV1) -> ArtifactRef:
        payload = snapshot.to_dict()
        artifact = Artifact.from_text(
            artifact_id=(
                f"phase16:project-snapshot:{_id_fragment(snapshot.goal_id)}:"
                f"e{snapshot.epoch}:{snapshot.diff_sha256[:16]}"
            ),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=canonical_json(payload),
            media_type="application/json",
            source="Phase16ProjectSnapshot",
            created_at="1970-01-01T00:00:00+00:00",
            metadata={
                "phase16_role": _PROJECT_SNAPSHOT,
                "goal_id": snapshot.goal_id,
                "epoch": snapshot.epoch,
                "head_commit": snapshot.head_commit,
                "diff_sha256": snapshot.diff_sha256,
            },
        )
        return self._ensure(artifact)

    def publish_verified_outcome(self, outcome: VerifiedTodoOutcomeV1) -> ArtifactRef:
        payload = outcome.to_dict()
        content = canonical_json(payload)
        if len(content) > self.policy.max_verified_outcome_chars:
            raise ValueError(
                f"verified outcome exceeds max_verified_outcome_chars={self.policy.max_verified_outcome_chars}"
            )
        artifact = Artifact.from_text(
            artifact_id=(
                f"phase16:verified-outcome:{_id_fragment(outcome.goal_id)}:"
                f"{_id_fragment(outcome.todo_id)}:e{outcome.epoch_after}"
            ),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=content,
            media_type="application/json",
            source="Phase16VerifiedTodoOutcome",
            created_at="1970-01-01T00:00:00+00:00",
            scope_tags=tuple(outcome.changed_paths),
            metadata={
                "phase16_role": _VERIFIED_TODO_OUTCOME,
                "goal_id": outcome.goal_id,
                "todo_id": outcome.todo_id,
                "run_id": outcome.run_id,
                "epoch_before": outcome.epoch_before,
                "epoch_after": outcome.epoch_after,
                "verification_report_id": outcome.verification_report_id,
                "verification_fingerprint": outcome.verification_fingerprint,
            },
        )
        return self._ensure(artifact)

    def publish_recovery_context(
        self,
        *,
        goal_id: str,
        todo_id: str,
        cycle: int,
        content: str,
        source: str = "Phase15Recovery",
    ) -> ArtifactRef:
        if cycle <= 0:
            raise ValueError("cycle must be > 0")
        artifact = Artifact.from_text(
            artifact_id=(
                f"phase16:recovery:{_id_fragment(goal_id)}:{_id_fragment(todo_id)}:"
                f"c{cycle}:{stable_sha256(content)[:16]}"
            ),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=content,
            media_type="application/json" if content.lstrip().startswith("{") else "text/plain",
            source=source,
            created_at="1970-01-01T00:00:00+00:00",
            metadata={
                "phase16_role": _RECOVERY_CONTEXT,
                "goal_id": goal_id,
                "todo_id": todo_id,
                "cycle": cycle,
            },
        )
        return self._ensure(artifact)

    def _artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self.artifact_store.get_artifact(ref.artifact_id) for ref in self.artifact_store.list_refs())

    def root_context(self, goal_id: str) -> RootContextRefs:
        source: Artifact | None = None
        capsule: Artifact | None = None
        for artifact in self._artifacts():
            if _metadata_text(artifact, "goal_id") != goal_id:
                continue
            role = _artifact_role(artifact)
            if role == _ROOT_SOURCE:
                source = artifact
            elif role == _ROOT_CAPSULE:
                capsule = artifact
        if source is None or capsule is None:
            raise ContextResolutionError(f"missing Phase 16 root context for goal {goal_id}")
        root_source = _metadata_text(capsule, "root_source_artifact_id")
        if root_source != source.artifact_id:
            raise ContextResolutionError("root capsule/source identity mismatch")
        return RootContextRefs(source.ref, capsule.ref)

    def latest_project_snapshot(self, goal_id: str) -> tuple[ProjectSnapshotV1, ArtifactRef]:
        candidates: list[Artifact] = []
        for artifact in self._artifacts():
            if _artifact_role(artifact) == _PROJECT_SNAPSHOT and _metadata_text(artifact, "goal_id") == goal_id:
                candidates.append(artifact)
        if not candidates:
            raise ContextResolutionError(f"missing Phase 16 project snapshot for goal {goal_id}")
        candidates.sort(key=lambda item: (_metadata_int(item, "epoch") or -1, item.artifact_id))
        latest = candidates[-1]
        try:
            raw = json.loads(latest.content)
            snapshot = ProjectSnapshotV1(
                str(raw["goal_id"]),
                int(raw["epoch"]),
                str(raw["head_commit"]),
                str(raw["diff_sha256"]),
                tuple(raw.get("dirty_paths", ())),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextResolutionError(f"invalid project snapshot artifact: {exc}") from exc
        return snapshot, latest.ref

    def verified_outcome(self, goal_id: str, todo_id: str) -> tuple[VerifiedTodoOutcomeV1, ArtifactRef]:
        candidates: list[Artifact] = []
        for artifact in self._artifacts():
            if (
                _artifact_role(artifact) == _VERIFIED_TODO_OUTCOME
                and _metadata_text(artifact, "goal_id") == goal_id
                and _metadata_text(artifact, "todo_id") == todo_id
            ):
                candidates.append(artifact)
        if not candidates:
            raise ContextResolutionError(f"missing verified dependency outcome for {todo_id}")
        candidates.sort(key=lambda item: (_metadata_int(item, "epoch_after") or -1, item.artifact_id))
        artifact = candidates[-1]
        try:
            raw = json.loads(artifact.content)
            outcome = VerifiedTodoOutcomeV1(
                str(raw["goal_id"]), str(raw["todo_id"]), str(raw["run_id"]),
                str(raw["objective"]), str(raw["summary"]),
                int(raw["epoch_before"]), int(raw["epoch_after"]),
                tuple(raw.get("changed_paths", ())),
                str(raw["verification_report_id"]), str(raw["verification_fingerprint"]),
                tuple(raw.get("invariants", ())), tuple(raw.get("evidence_ids", ())),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextResolutionError(f"invalid verified outcome artifact: {exc}") from exc
        return outcome, artifact.ref

    def verified_outcome_ref(self, goal_id: str, todo_id: str) -> ArtifactRef:
        return self.verified_outcome(goal_id, todo_id)[1]

    @staticmethod
    def _scope_allowed(artifact: Artifact, contract: WorkContract) -> bool:
        tags = set(artifact.scope_tags)
        forbidden = set(contract.forbidden_scope)
        allowed = set(contract.allowed_scope)
        if tags & forbidden:
            return False
        if allowed and not (tags & allowed):
            return False
        return True

    def _context_item(
        self,
        *,
        item_id: str,
        source: ContextSource,
        content: str,
        required: bool,
        priority: int,
        provenance: str,
        ref: ArtifactRef | None = None,
    ) -> ContextItem:
        return ContextItem(
            item_id=item_id,
            source=source,
            content=content,
            required=required,
            priority=priority,
            token_estimate=self.token_estimator(content),
            provenance=provenance,
            artifact_ref=ref,
        )

    def build_pack(
        self,
        *,
        goal_id: str,
        todo_id: str,
        objective: str,
        contract: WorkContract,
        profile_id: str,
        dependency_todo_ids: tuple[str, ...] = (),
        optional_repo_artifacts: tuple[ArtifactRef, ...] = (),
        recovery_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ContextPackV1:
        if contract.goal_id != goal_id or contract.todo_id != todo_id:
            raise ContextResolutionError("Phase 16 context identity does not match WorkContract")
        if tuple(dependency_todo_ids) != tuple(contract_dependency for contract_dependency in dependency_todo_ids):
            raise ContextResolutionError("invalid dependency_todo_ids")
        if len(dependency_todo_ids) > self.policy.max_dependency_outcomes:
            raise ContextResolutionError("dependency outcome count exceeds Phase 16 policy")
        profile = self.policy.profile(profile_id)
        budget = ContextBudget(profile.initial_pack_tokens, profile.max_items, profile.max_bytes)
        snapshot, snapshot_ref = self.latest_project_snapshot(goal_id)
        dependency_resolution = self.dependency_context(goal_id,todo_id)
        if dependency_resolution is not None:
            snapshot,snapshot_ref,pinned_dependency_refs=dependency_resolution
        dependency_refs = (list(pinned_dependency_refs) if dependency_resolution is not None else
            [self.verified_outcome_ref(goal_id, dependency) for dependency in dependency_todo_ids])
        root_ref = self.required_root_ref(goal_id=goal_id, todo_id=todo_id, objective=objective,
            contract=contract, dependency_refs=dependency_refs, profile_id=profile_id)
        required_refs: list[ArtifactRef] = [root_ref, snapshot_ref]
        required_refs.extend(dependency_refs)
        required_refs.extend(recovery_artifacts)
        if len({ref.artifact_id for ref in required_refs}) != len(required_refs):
            raise ContextResolutionError("duplicate required Phase 16 context artifact")

        items: list[ContextItem] = [
            self._context_item(
                item_id=f"phase16:{todo_id}:todo",
                source=ContextSource.TODO,
                content=objective,
                required=True,
                priority=1000,
                provenance=f"todo:{todo_id}",
            ),
            self._context_item(
                item_id=f"phase16:{todo_id}:contract",
                source=ContextSource.WORK_CONTRACT,
                content=canonical_json(contract.to_dict()),
                required=True,
                priority=950,
                provenance=f"work_contract:{todo_id}",
            ),
        ]
        for order, ref in enumerate(required_refs):
            artifact = self.artifact_store.get_artifact(ref.artifact_id)
            if artifact.ref != ref:
                raise ContextResolutionError(f"required artifact ref mismatch: {ref.artifact_id}")
            source = ContextSource.DEPENDENCY_EVIDENCE if ref in dependency_refs else ContextSource.ARTIFACT
            items.append(self._context_item(
                item_id=f"phase16:{todo_id}:required:{order}:{ref.artifact_id}",
                source=source,
                content=artifact.content,
                required=True,
                priority=900 - order,
                provenance=f"artifact:{artifact.artifact_id}@{artifact.sha256}",
                ref=artifact.ref,
            ))

        if not self._fits(items, budget):
            raise ContextBudgetError(
                f"required Phase 16 context cannot fit profile={profile_id} budget={profile.initial_pack_tokens}"
            )

        optional = list(optional_repo_artifacts[: self.policy.max_optional_repo_artifacts])
        ranked: list[tuple[float, str, Artifact]] = []
        omitted: list[str] = []
        selected_hashes = {item.artifact_ref.sha256 for item in items if item.artifact_ref is not None}
        for ref in optional:
            try:
                artifact = self.artifact_store.get_artifact(ref.artifact_id)
            except Exception:
                omitted.append(ref.artifact_id)
                continue
            if artifact.ref != ref or not self._scope_allowed(artifact, contract) or artifact.sha256 in selected_hashes:
                omitted.append(ref.artifact_id)
                continue
            score = artifact.metadata.get("score", 0.0)
            score_value = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
            ranked.append((-score_value, artifact.artifact_id, artifact))
        ranked.sort(key=lambda row: (row[0], row[1]))
        for _, _, artifact in ranked:
            candidate = self._context_item(
                item_id=f"phase16:{todo_id}:optional:{artifact.artifact_id}",
                source=ContextSource.ARTIFACT,
                content=artifact.content,
                required=False,
                priority=100,
                provenance=f"artifact:{artifact.artifact_id}@{artifact.sha256}",
                ref=artifact.ref,
            )
            if not self._fits([*items, candidate], budget):
                omitted.append(artifact.artifact_id)
                continue
            items.append(candidate)
            selected_hashes.add(artifact.sha256)

        bundle = ContextBundle(
            bundle_id=f"phase16:{goal_id}:{todo_id}:{profile_id}:e{snapshot.epoch}",
            request_id=f"phase16:{goal_id}:{todo_id}:{profile_id}:e{snapshot.epoch}",
            goal_id=goal_id,
            todo_id=todo_id,
            items=tuple(items),
            budget=budget,
            required_artifact_ids=tuple(ref.artifact_id for ref in required_refs),
            omitted_optional_artifact_ids=tuple(sorted(set(omitted))),
        )
        rendered = self.render_bundle(bundle, snapshot_epoch=snapshot.epoch)
        selected_ids = tuple(
            item.artifact_ref.artifact_id for item in bundle.items if item.artifact_ref is not None
        )
        fingerprint = stable_sha256({
            "goal_id": goal_id,
            "todo_id": todo_id,
            "profile_id": profile_id,
            "project_epoch": snapshot.epoch,
            "items": [
                {
                    "item_id": item.item_id,
                    "required": item.required,
                    "provenance": item.provenance,
                    "content_sha256": stable_sha256(item.content),
                }
                for item in bundle.items
            ],
        })
        pack = ContextPackV1(
            goal_id,
            todo_id,
            profile_id,
            snapshot.epoch,
            fingerprint,
            bundle.used_tokens,
            budget.max_tokens,
            tuple(ref.artifact_id for ref in required_refs),
            selected_ids,
            bundle.omitted_optional_artifact_ids,
            rendered,
        )
        if callable(getattr(self,'dependency_context_observer',None)):
            self.dependency_context_observer(goal_id,todo_id,objective,pack)
        from .root_projection import VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1 as projection_key
        if projection_key in contract.constraints:
            self.root_projection_authority.record_pack(dict(goal_id=goal_id, todo_id=todo_id,
                objective=objective, profile_id=profile_id, recovery_artifacts=recovery_artifacts), pack)
        return pack

    @staticmethod
    def _fits(items: Iterable[ContextItem], budget: ContextBudget) -> bool:
        values = tuple(items)
        return (
            len(values) <= budget.max_items
            and sum(item.token_estimate for item in values) <= budget.max_tokens
            and sum(item.size_bytes for item in values) <= budget.max_bytes
        )

    @staticmethod
    def render_bundle(bundle: ContextBundle, *, snapshot_epoch: int) -> str:
        rows = [
            "EXTERNAL_CONTEXT_V1",
            f"goal_id={bundle.goal_id}",
            f"todo_id={bundle.todo_id}",
            f"project_epoch={snapshot_epoch}",
            f"context_tokens={bundle.used_tokens}/{bundle.budget.max_tokens}",
            "RULES:",
            "- The CURRENT TODO and WorkContract are the execution authority.",
            "- ROOT_GOAL_CAPSULE preserves global intent; do not broaden scope beyond the current TODO.",
            "- VERIFIED_TODO_OUTCOME items are trusted dependency facts from deterministic verification.",
            "- Repository items are a bounded working set, not the whole project.",
            "- If evidence is insufficient, request targeted context expansion; do not guess.",
        ]
        for index, item in enumerate(bundle.items, 1):
            marker = "REQUIRED" if item.required else "OPTIONAL"
            rows.extend((
                f"--- CONTEXT_ITEM {index} [{marker}] source={item.source.value} ---",
                f"provenance={item.provenance}",
                item.content,
            ))
        rows.append("END_EXTERNAL_CONTEXT_V1")
        return "\n".join(rows)

    @staticmethod
    def objective_with_pack(objective: str, pack: ContextPackV1) -> str:
        return (
            "CURRENT_TASK:\n"
            + objective.strip()
            + "\n\n"
            + pack.rendered_text
            + "\n\nEXECUTION_INSTRUCTION:\n"
            + "Solve only CURRENT_TASK using the bounded external context above. "
              "Do not reconstruct project state from memory. Read current workspace files before mutation."
        )
