"""Deterministic ContextBundle assembly over explicit ArtifactRefs."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable

from ..observability import ContextBuildObserver

from .errors import ArtifactNotFoundError, ContextBudgetError, ContextResolutionError
from .models import (
    Artifact,
    ArtifactRef,
    ContextBundle,
    ContextItem,
    ContextRequest,
    ContextSource,
)
from .store import ArtifactStore
from .token_estimator import Utf8TokenEstimator


class ContextBuilder:
    """Build a bounded ContextBundle without retrieval, routing, or execution.

    Required content is fail-closed: it must resolve, pass scope checks, match
    its ArtifactRef, and fit the hard budget. Optional content is included in a
    stable artifact-id order when it fits; missing, out-of-scope, duplicate
    content, and over-budget optional items are recorded as omitted.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        token_estimator: Callable[[str], int] | None = None,
    ) -> None:
        if not isinstance(artifact_store, ArtifactStore):
            raise ValueError("artifact_store must implement ArtifactStore")
        self.artifact_store = artifact_store
        self.token_estimator = token_estimator or Utf8TokenEstimator()

    def build(
        self,
        request: ContextRequest,
        *,
        dependency_artifact_ids: Iterable[str] = (),
        observer: ContextBuildObserver | None = None,
    ) -> ContextBundle:
        if not isinstance(request, ContextRequest):
            raise ValueError("request must be a ContextRequest")
        dependency_ids = self._dependency_ids(dependency_artifact_ids, request)
        if observer is not None:
            observer.build_started(request=request, budget=request.budget.to_dict())

        items: list[ContextItem] = [
            self._text_item(
                f"{request.request_id}:todo",
                ContextSource.TODO,
                request.objective,
                True,
                1000,
                f"todo:{request.todo_id}",
            ),
            self._text_item(
                f"{request.request_id}:work-contract",
                ContextSource.WORK_CONTRACT,
                json.dumps(
                    request.work_contract.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                True,
                900,
                f"work_contract:{request.todo_id}",
            ),
        ]

        selected_hashes: set[str] = set()
        for ref in sorted(request.required_artifacts, key=lambda value: value.artifact_id):
            artifact = self._resolve_required(ref)
            self._require_scope(artifact, request)
            source = (
                ContextSource.DEPENDENCY_EVIDENCE
                if ref.artifact_id in dependency_ids
                else ContextSource.ARTIFACT
            )
            items.append(self._artifact_item(request, artifact, True, 800, source))
            selected_hashes.add(artifact.sha256)

        if observer is not None:
            required_items = tuple(items)
            required_total = self._observer_totals(required_items)
            observer.budget_decision(
                request=request, required_items=required_items, optional_items=(), selected_items=required_items,
                omitted_items=(), required_total=required_total, selected_total=required_total,
                overflow=self._observer_overflow(required_total, request),
                top_required_contributors=self._observer_top_required(required_items),
                status="OVER_BUDGET" if not self._fits(items, request) else "FIT",
            )
        self._require_budget(items, request)

        omitted: list[str] = []
        for ref in sorted(request.optional_artifacts, key=lambda value: value.artifact_id):
            try:
                artifact = self.artifact_store.get_artifact(ref.artifact_id)
            except ArtifactNotFoundError:
                omitted.append(ref.artifact_id)
                continue
            self._require_ref_match(ref, artifact)
            if not self._scope_allowed(artifact, request):
                omitted.append(ref.artifact_id)
                continue
            if artifact.sha256 in selected_hashes:
                omitted.append(ref.artifact_id)
                continue
            candidate = self._artifact_item(request, artifact, False, 100, ContextSource.ARTIFACT)
            if not self._fits(items + [candidate], request):
                omitted.append(ref.artifact_id)
                continue
            items.append(candidate)
            selected_hashes.add(artifact.sha256)

        return ContextBundle(
            bundle_id=f"{request.request_id}:bundle",
            request_id=request.request_id,
            goal_id=request.goal_id,
            todo_id=request.todo_id,
            items=tuple(items),
            budget=request.budget,
            required_artifact_ids=tuple(
                ref.artifact_id
                for ref in sorted(request.required_artifacts, key=lambda value: value.artifact_id)
            ),
            omitted_optional_artifact_ids=tuple(omitted),
        )

    @staticmethod
    def _observer_totals(items: Iterable[ContextItem]) -> dict[str, int]:
        values = tuple(items)
        return {"tokens": sum(item.token_estimate for item in values), "bytes": sum(item.size_bytes for item in values), "items": len(values)}

    @staticmethod
    def _observer_overflow(totals: dict[str, int], request: ContextRequest) -> dict[str, int]:
        return {"tokens": max(0, totals["tokens"] - request.budget.max_tokens), "bytes": max(0, totals["bytes"] - request.budget.max_bytes), "items": max(0, totals["items"] - request.budget.max_items)}

    @staticmethod
    def _observer_top_required(items: tuple[ContextItem, ...]) -> tuple[ContextItem, ...]:
        return tuple(sorted(items, key=lambda item: (-item.token_estimate, -item.size_bytes, item.item_id))[:5])

    def _text_item(
        self,
        item_id: str,
        source: ContextSource,
        content: str,
        required: bool,
        priority: int,
        provenance: str,
    ) -> ContextItem:
        estimate = self.token_estimator(content)
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 0:
            raise ContextResolutionError("token estimator must return an integer >= 0")
        return ContextItem(item_id, source, content, required, priority, estimate, provenance)

    def _artifact_item(
        self,
        request: ContextRequest,
        artifact: Artifact,
        required: bool,
        priority: int,
        source: ContextSource,
    ) -> ContextItem:
        estimate = self.token_estimator(artifact.content)
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 0:
            raise ContextResolutionError("token estimator must return an integer >= 0")
        return ContextItem(
            item_id=f"{request.request_id}:artifact:{artifact.artifact_id}",
            source=source,
            content=artifact.content,
            required=required,
            priority=priority,
            token_estimate=estimate,
            provenance=f"artifact:{artifact.artifact_id}@{artifact.sha256}",
            artifact_ref=artifact.ref,
        )

    def _resolve_required(self, ref: ArtifactRef) -> Artifact:
        try:
            artifact = self.artifact_store.get_artifact(ref.artifact_id)
        except ArtifactNotFoundError as exc:
            raise ContextResolutionError(f"missing required artifact: {ref.artifact_id}") from exc
        self._require_ref_match(ref, artifact)
        return artifact

    @staticmethod
    def _require_ref_match(ref: ArtifactRef, artifact: Artifact) -> None:
        if artifact.ref != ref:
            raise ContextResolutionError(f"artifact ref integrity mismatch: {ref.artifact_id}")

    @staticmethod
    def _scope_allowed(artifact: Artifact, request: ContextRequest) -> bool:
        tags = set(artifact.scope_tags)
        forbidden = set(request.work_contract.forbidden_scope)
        allowed = set(request.work_contract.allowed_scope)
        if tags & forbidden:
            return False
        if allowed and not (tags & allowed):
            return False
        return True

    def _require_scope(self, artifact: Artifact, request: ContextRequest) -> None:
        if not self._scope_allowed(artifact, request):
            raise ContextResolutionError(
                f"required artifact is outside WorkContract scope: {artifact.artifact_id}"
            )

    @staticmethod
    def _fits(items: list[ContextItem], request: ContextRequest) -> bool:
        return (
            len(items) <= request.budget.max_items
            and sum(item.token_estimate for item in items) <= request.budget.max_tokens
            and sum(item.size_bytes for item in items) <= request.budget.max_bytes
        )

    def _require_budget(self, items: list[ContextItem], request: ContextRequest) -> None:
        if not self._fits(items, request):
            raise ContextBudgetError("required context cannot fit within the hard ContextBudget")

    @staticmethod
    def _dependency_ids(values: Iterable[str], request: ContextRequest) -> frozenset[str]:
        try:
            normalized = tuple(values)
        except TypeError as exc:
            raise ContextResolutionError("dependency_artifact_ids must be iterable") from exc
        if any(not isinstance(value, str) or not value.strip() for value in normalized):
            raise ContextResolutionError("dependency_artifact_ids must contain non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise ContextResolutionError("dependency_artifact_ids must not contain duplicates")
        required_ids = {ref.artifact_id for ref in request.required_artifacts}
        unknown = set(normalized) - required_ids
        if unknown:
            raise ContextResolutionError(
                f"dependency_artifact_ids must be required artifacts: {sorted(unknown)}"
            )
        return frozenset(normalized)
