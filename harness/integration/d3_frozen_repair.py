"""Additive RC-7B.D3-F production safeguards; frozen core remains untouched."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..repo_intelligence.models import (
    RepositoryFileKind,
    RepositoryIndex,
    RepositoryRetrievalHit,
    RepositoryRetrievalResult,
)
from ..workspace.models import ToolName, ToolResult, ToolStatus

SEARCH_TEXT_WORKER_GUIDANCE = (
    "PRODUCTION SEARCH_TEXT CONTRACT: query is required and must be concrete/non-empty. "
    "Omit path or use path=\"\" for repository root; use a real repository-relative "
    "path for scoped search. The literal text 'relative/path or empty' is forbidden."
)
_PLACEHOLDER = "relative/path or empty"
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class SemanticDiscoveryStop(RuntimeError):
    """Legacy typed stop name; production dispatch now returns a ToolResult."""


def _words(value: str) -> set[str]:
    values = {token.casefold() for token in _TOKEN.findall(value)}
    for token in tuple(values):
        if len(token) > 3 and token.endswith("s"):
            values.add(token[:-1])
        elif len(token) > 2:
            values.add(token + "s")
    return values


@dataclass
class ProductionSearchTextContractDispatcher:
    """Production-only dispatcher boundary for malformed and unproductive discovery."""

    inner: Any
    max_no_progress: int = 3
    _counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def _key(self, request) -> tuple[str, str]:
        return request.run_id, request.todo_id

    def _reset(self, request) -> None:
        self._counts.pop(self._key(request), None)

    def _observe_no_progress(self, request) -> int:
        """Return the durable-stop counter; never raise through AgentLoop."""
        key = self._key(request)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count

    def _metadata(self, *, count: int, original_status: ToolStatus | None = None) -> dict[str, object]:
        metadata: dict[str, object] = {
            "d3_search_contract": True,
            "semantic_discovery_no_progress": True,
            "no_progress_count": count,
        }
        if original_status is not None:
            metadata["original_status"] = original_status.value
        if count >= self.max_no_progress:
            metadata.update({
                "semantic_discovery_stop": True,
                "failure_kind": "TOOL_LOOP",
            })
        return metadata

    def _invalid(self, request, reason: str, *, count: int) -> ToolResult:
        return ToolResult(
            request.call_id, request.run_id, request.todo_id, request.tool,
            ToolStatus.INVALID, reason, self._metadata(count=count), (),
        )

    def _unproductive(self, request, result: ToolResult, *, count: int) -> ToolResult:
        """Normalize zero-hit SUCCESS into a durable non-success result."""
        metadata = dict(result.metadata)
        metadata.update(self._metadata(count=count, original_status=result.status))
        status = ToolStatus.NOT_FOUND if result.status is ToolStatus.SUCCESS else result.status
        return replace(result, status=status, metadata=metadata)

    def execute(self, session, request):
        if request.tool is ToolName.READ_TEXT:
            result = self.inner.execute(session, request)
            if result.status is ToolStatus.SUCCESS:
                self._reset(request)
            return result
        if request.tool in {ToolName.WRITE_TEXT, ToolName.REPLACE_TEXT, ToolName.DELETE_FILE}:
            result = self.inner.execute(session, request)
            if result.changed_paths:
                self._reset(request)
            return result
        if request.tool is not ToolName.SEARCH_TEXT:
            return self.inner.execute(session, request)
        query = request.arguments.get("query")
        path = request.arguments.get("path")
        if not isinstance(query, str) or not query.strip():
            count = self._observe_no_progress(request)
            return self._invalid(request, SEARCH_TEXT_WORKER_GUIDANCE, count=count)
        if isinstance(path, str) and path.strip().casefold() == _PLACEHOLDER:
            count = self._observe_no_progress(request)
            return self._invalid(request, SEARCH_TEXT_WORKER_GUIDANCE, count=count)
        result = self.inner.execute(session, request)
        match_count = result.metadata.get("match_count")
        if result.status is ToolStatus.NOT_FOUND or (
            result.status is ToolStatus.SUCCESS
            and isinstance(match_count, int)
            and not isinstance(match_count, bool)
            and match_count == 0
        ):
            count = self._observe_no_progress(request)
            return self._unproductive(request, result, count=count)
        if result.status is ToolStatus.SUCCESS:
            self._reset(request)
        return result


def repair_repository_grounding(
    index: RepositoryIndex,
    retrieval: RepositoryRetrievalResult,
    query: str,
    *,
    max_results: int,
    max_bytes: int,
) -> RepositoryRetrievalResult:
    """Supplement weak frozen retrieval with deterministic bounded production paths."""
    wanted = _words(query)
    records = index.file_map
    selected = list(retrieval.hits)
    selected_paths = {hit.path for hit in selected}
    total = sum(hit.size_bytes for hit in selected)

    def content_tokens(path: str) -> set[str]:
        record = records[path]
        if record.language != "python":
            return set()
        try:
            return _words((Path(index.repo_root) / path).read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError):
            return set()

    def add(record, reason: str, score: int) -> None:
        nonlocal total
        if record.path in selected_paths or len(selected) >= max_results:
            return
        if total + record.size_bytes > max_bytes:
            return
        selected.append(RepositoryRetrievalHit(record.path, score, (reason,), record.size_bytes, record.sha256))
        selected_paths.add(record.path)
        total += record.size_bytes

    for record in index.files:
        path_words = _words(record.path)
        source_words = content_tokens(record.path)
        relevant_model = bool({"task", "model", "dataclass"} & wanted) and "model" in path_words
        relevant_store = bool({"taskstore", "store"} & wanted) and ("store" in path_words or "taskstore" in source_words)
        relevant_test = ("test" in path_words and ("store" in path_words or "taskstore" in source_words))
        if relevant_model or relevant_store or relevant_test:
            add(record, "d3f:bounded_grounding", 900)

    if not selected:
        for record in index.files:
            if record.kind in {RepositoryFileKind.SOURCE, RepositoryFileKind.TEST}:
                add(record, "d3f:fallback_no_semantic_hits", 1)
    return RepositoryRetrievalResult.create(
        index_id=index.index_id,
        query=retrieval.query,
        explicit_paths=retrieval.explicit_paths,
        hits=tuple(selected),
    )
