'Explainable deterministic retrieval over a repository intelligence RepositoryIndex.'
from __future__ import annotations

import re
from pathlib import Path

from .models import RepositoryFileKind, RepositoryIndex, RepositoryIntelligencePolicy, RepositoryRetrievalHit, RepositoryRetrievalResult

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        lower = raw.casefold()
        result.add(lower)
        result.update(part for part in lower.split("_") if part)
        # camel-ish split without locale dependence
        result.update(x.casefold() for x in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", raw) if x)
    return {x for x in result if len(x) >= 2}


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


class RepositoryRetriever:
    def __init__(self, *, policy: RepositoryIntelligencePolicy | None = None) -> None:
        self.policy = policy or RepositoryIntelligencePolicy()

    def retrieve(self, index: RepositoryIndex, query: str, *, explicit_paths: tuple[str, ...] = ()) -> RepositoryRetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be non-empty string")
        normalized_explicit = tuple(dict.fromkeys(_normalize_path(x) for x in explicit_paths if isinstance(x, str) and x.strip()))
        qtokens = _tokens(query)
        base: dict[str, int] = {}
        reasons: dict[str, set[str]] = {}
        file_map = index.file_map

        def add(path: str, amount: int, reason: str) -> None:
            base[path] = base.get(path, 0) + amount
            reasons.setdefault(path, set()).add(reason)

        for file in index.files:
            path_lower = file.path.casefold()
            name_tokens = _tokens(Path(file.path).name)
            path_tokens = _tokens(file.path)
            for explicit in normalized_explicit:
                exp_lower = explicit.casefold()
                if path_lower == exp_lower:
                    add(file.path, 10000, f"explicit_path:{explicit}")
                elif path_lower.startswith(exp_lower.rstrip("/") + "/"):
                    add(file.path, 1800, f"explicit_scope:{explicit}")
            common_name = sorted(qtokens & name_tokens)
            common_path = sorted(qtokens & path_tokens)
            if common_name:
                add(file.path, 350 * len(common_name), "filename_terms:" + ",".join(common_name))
            if common_path:
                add(file.path, 140 * len(common_path), "path_terms:" + ",".join(common_path))
            for symbol in file.symbols:
                stokens = _tokens(symbol.name) | _tokens(symbol.qualified_name)
                common = sorted(qtokens & stokens)
                if common:
                    add(file.path, 520 * len(common), f"symbol:{symbol.qualified_name}")
            for imp in file.imports:
                itokens = _tokens(imp.module) | set().union(*(_tokens(x) for x in imp.names)) if imp.names else _tokens(imp.module)
                common = sorted(qtokens & itokens)
                if common:
                    add(file.path, 80 * len(common), "import_terms:" + ",".join(common))
            if "test" in qtokens or "tests" in qtokens:
                if file.kind is RepositoryFileKind.TEST:
                    add(file.path, 160, "query_mentions_tests")

        # Propagate only from directly relevant files. This keeps retrieval explainable
        # and ensures associated tests/source/dependencies are brought into context.
        direct = {path for path, score in base.items() if score > 0}
        for assoc in index.test_associations:
            if assoc.source_path in direct:
                add(assoc.test_path, 280, f"associated_test_of:{assoc.source_path}")
            if assoc.test_path in direct:
                add(assoc.source_path, 280, f"source_under_test:{assoc.test_path}")
        for edge in index.dependency_edges:
            if edge.source_path in direct:
                add(edge.target_path, 90, f"dependency_of:{edge.source_path}")
            if edge.target_path in direct:
                add(edge.source_path, 45, f"dependent_on:{edge.target_path}")

        ranked = sorted(
            ((path, score) for path, score in base.items() if score > 0),
            key=lambda item: (-item[1], item[0].casefold()),
        )
        hits: list[RepositoryRetrievalHit] = []
        total = 0
        file_limit = min(self.policy.max_results, self.policy.max_context_files)
        for path, score in ranked:
            file = file_map[path]
            if len(hits) >= file_limit:
                break
            if total + file.size_bytes > self.policy.max_context_bytes:
                continue
            hits.append(RepositoryRetrievalHit(path, score, tuple(sorted(reasons[path], key=str.casefold)), file.size_bytes, file.sha256))
            total += file.size_bytes
        return RepositoryRetrievalResult.create(index_id=index.index_id, query=query.strip(), explicit_paths=normalized_explicit, hits=tuple(hits))
