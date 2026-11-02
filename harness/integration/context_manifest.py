'Production-only workflow context telemetry and recovery projection seam.'
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.errors import ContextBudgetError
from ..workflow.models import canonical_json
from .recovery_context_projection import persist_projection, projection_from_raw


@dataclass(frozen=True, slots=True)
class WorkflowContextManifestV1:
    schema_version: int
    goal_id: str
    todo_id: str
    profile_id: str
    status: str
    payload: dict[str, Any]

    def canonical_json(self) -> str:
        return canonical_json({"schema_version": self.schema_version, "goal_id": self.goal_id,
                               "todo_id": self.todo_id, "profile_id": self.profile_id,
                               "status": self.status, **self.payload})

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ProductionContextSpine:
    """Decorator: retain raw recovery evidence but expose only projections."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.artifact_store = inner.artifact_store
        self.policy = inner.policy
        self.token_estimator = inner.token_estimator

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    @staticmethod
    def _item(ref, artifact, *, role: str, estimator) -> dict[str, Any]:
        content = artifact.content
        return {"artifact_id": ref.artifact_id, "role": role, "source": artifact.source,
                "estimated_tokens": estimator(content), "bytes": len(content.encode("utf-8"))}

    def _persist_manifest(self, manifest: WorkflowContextManifestV1) -> None:
        content = manifest.canonical_json()
        artifact_id = f"phase21-3:context-manifest:{manifest.fingerprint[:32]}"
        artifact = Artifact.from_text(
            artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE, content=content, media_type="application/json",
            source="Phase16ContextManifestV1", created_at="1970-01-01T00:00:00+00:00",
            metadata={"production_role": "PHASE16_CONTEXT_MANIFEST", "goal_id": manifest.goal_id,
                      "todo_id": manifest.todo_id, "profile_id": manifest.profile_id,
                      "status": manifest.status, "fingerprint": manifest.fingerprint},
        )
        if not self.artifact_store.has_artifact(artifact_id):
            self.artifact_store.create_artifact(artifact)

    def _overflow_payload(self, *, goal_id, todo_id, objective, contract, profile_id, dependency_todo_ids, recovery_artifacts):
        profile = self.policy.profile(profile_id)
        roots = self.inner.root_context(goal_id)
        _, snapshot_ref = self.inner.latest_project_snapshot(goal_id)
        refs = [roots.capsule, snapshot_ref]
        resolved=self.inner.dependency_context(goal_id,todo_id)
        if resolved is not None:
            refs=[roots.capsule,resolved[1],*resolved[2]]
        else:
            refs.extend(self.inner.verified_outcome_ref(goal_id, item) for item in dependency_todo_ids)
        refs[0] = self.inner.required_root_ref(goal_id=goal_id, todo_id=todo_id, objective=objective,
            contract=contract, dependency_refs=refs[2:],
            profile_id=profile_id)
        refs.extend(recovery_artifacts)
        rows = [
            {"artifact_id": f"phase16:{todo_id}:todo", "role": "TODO", "source": "objective",
             "estimated_tokens": self.token_estimator(objective), "bytes": len(objective.encode("utf-8"))},
            {"artifact_id": f"phase16:{todo_id}:contract", "role": "WORK_CONTRACT", "source": "contract",
             "estimated_tokens": self.token_estimator(canonical_json(contract.to_dict())),
             "bytes": len(canonical_json(contract.to_dict()).encode("utf-8"))},
        ]
        for ref in refs:
            artifact = self.artifact_store.get_artifact(ref.artifact_id)
            rows.append(self._item(ref, artifact, role="RECOVERY" if artifact.metadata.get("production_role") == "RECOVERY_CONTEXT_PROJECTION" else "REQUIRED_ARTIFACT", estimator=self.token_estimator))
        total_tokens = sum(row["estimated_tokens"] for row in rows)
        total_bytes = sum(row["bytes"] for row in rows)
        return {"budget": {"max_tokens": profile.initial_pack_tokens, "max_items": profile.max_items, "max_bytes": profile.max_bytes},
                "required_total": {"items": len(rows), "estimated_tokens": total_tokens, "bytes": total_bytes},
                "optional_total": {"items": 0, "estimated_tokens": 0, "bytes": 0},
                "selected_total": {"items": 0, "estimated_tokens": 0, "bytes": 0},
                "required_items": rows, "optional_selected": [], "optional_omitted": [],
                "top_required_contributors": sorted(rows, key=lambda row: (-row["bytes"], row["artifact_id"]))[:5],
                "context_bundle_id": None}

    def build_pack(self, *, goal_id, todo_id, objective, contract, profile_id, dependency_todo_ids=(), optional_repo_artifacts=(), recovery_artifacts=()):
        try:
            bundle = self.inner.build_pack(goal_id=goal_id, todo_id=todo_id, objective=objective, contract=contract,
                                           profile_id=profile_id, dependency_todo_ids=dependency_todo_ids,
                                           optional_repo_artifacts=optional_repo_artifacts, recovery_artifacts=recovery_artifacts)
        except ContextBudgetError as exc:
            payload = self._overflow_payload(goal_id=goal_id, todo_id=todo_id, objective=objective, contract=contract,
                                             profile_id=profile_id, dependency_todo_ids=dependency_todo_ids,
                                             recovery_artifacts=recovery_artifacts)
            manifest = WorkflowContextManifestV1(1, goal_id, todo_id, profile_id, "REQUIRED_OVERFLOW", payload)
            self._persist_manifest(manifest)
            raise ContextBudgetError(
                f"required Phase16 context cannot fit: profile={profile_id} required_tokens={payload['required_total']['estimated_tokens']} budget_tokens={payload['budget']['max_tokens']} required_bytes={payload['required_total']['bytes']} top_contributors={[row['artifact_id'] for row in payload['top_required_contributors']]}"
            ) from exc
        required_ids = set(bundle.required_artifact_ids)
        def row(artifact_id, required):
            if artifact_id in required_ids or artifact_id in bundle.selected_artifact_ids:
                artifact = self.artifact_store.get_artifact(artifact_id)
                content = artifact.content
                source = artifact.source
            else:
                content, source = "", "inline"
            return {"artifact_id": artifact_id, "role": "REQUIRED_ARTIFACT" if required else "OPTIONAL_ARTIFACT",
                    "source": source, "estimated_tokens": self.token_estimator(content),
                    "bytes": len(content.encode("utf-8"))}
        inline_rows = [
            {"artifact_id": f"phase16:{todo_id}:todo", "role": "TODO", "source": "objective", "estimated_tokens": self.token_estimator(objective), "bytes": len(objective.encode("utf-8"))},
            {"artifact_id": f"phase16:{todo_id}:contract", "role": "WORK_CONTRACT", "source": "contract", "estimated_tokens": self.token_estimator(canonical_json(contract.to_dict())), "bytes": len(canonical_json(contract.to_dict()).encode("utf-8"))},
        ]
        required_rows = [*inline_rows, *[row(item, True) for item in bundle.required_artifact_ids]]
        optional_rows = [row(item, False) for item in bundle.selected_artifact_ids if item not in required_ids]
        totals = lambda rows: {"items": len(rows), "estimated_tokens": sum(x["estimated_tokens"] for x in rows), "bytes": sum(x["bytes"] for x in rows)}
        payload = {"budget": {"max_tokens": bundle.max_tokens, "max_items": self.policy.profile(profile_id).max_items, "max_bytes": self.policy.profile(profile_id).max_bytes},
                   "required_total": totals(required_rows), "optional_total": totals(optional_rows),
                   "selected_total": {"items": len(required_rows) + len(optional_rows), "estimated_tokens": bundle.used_tokens, "bytes": sum(x["bytes"] for x in [*required_rows, *optional_rows])},
                   "required_items": required_rows, "optional_selected": optional_rows,
                   "optional_omitted": [{"artifact_id": item, "role": "ARTIFACT", "reason": "budget_or_scope"} for item in bundle.omitted_optional_artifact_ids],
                   "top_required_contributors": sorted(required_rows, key=lambda x: (-x["bytes"], x["artifact_id"]))[:5],
                   "context_bundle_id": f"phase16:{goal_id}:{todo_id}:{profile_id}:e{bundle.project_epoch}"}
        self._persist_manifest(WorkflowContextManifestV1(1, goal_id, todo_id, profile_id, "FIT", payload))
        return bundle

    def publish_recovery_context(self, *, goal_id, todo_id, cycle, content, source="Phase15Recovery"):
        raw_ref = self.inner.publish_recovery_context(goal_id=goal_id, todo_id=todo_id, cycle=cycle, content=content, source=source)
        projection = projection_from_raw(raw_recovery_artifact_id=raw_ref.artifact_id, raw_content=content)
        return persist_projection(self.artifact_store, projection)

# Verified compatibility aliases for historical Python interfaces.
Phase16ContextManifestV1 = WorkflowContextManifestV1
