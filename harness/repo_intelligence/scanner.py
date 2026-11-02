"""Deterministic read-only repository indexing for isolated candidate worktrees."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

from ..worktree.manager import GitWorktreeManager
from .errors import RepositoryBudgetError, RepositoryIntegrityError, RepositorySnapshotMismatchError
from .models import (
    RepositoryDependencyEdge,
    RepositoryFile,
    RepositoryFileKind,
    RepositoryImport,
    RepositoryIndex,
    RepositoryIntelligencePolicy,
    RepositorySymbol,
    RepositorySymbolKind,
    RepositoryTestAssociation,
)

_TEXT_EXTENSIONS = {
    ".py", ".pyi", ".txt", ".md", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".html", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".sh", ".ps1", ".sql", ".graphql", ".proto", ".gradle",
}
_SOURCE_EXTENSIONS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".sh", ".ps1", ".sql"}
_CONFIG_NAMES = {"pyproject.toml", "package.json", "tsconfig.json", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "dockerfile", "makefile"}


def _language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
        ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
        ".cs": "csharp", ".sh": "shell", ".ps1": "powershell", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
    }.get(suffix, "text")


def _kind(path: str) -> RepositoryFileKind:
    p = Path(path)
    lower_parts = tuple(part.lower() for part in p.parts)
    name = p.name.lower()
    suffix = p.suffix.lower()
    if "tests" in lower_parts or "test" in lower_parts or name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.ts") or name.endswith(".spec.ts"):
        return RepositoryFileKind.TEST
    if "config" in lower_parts or name in _CONFIG_NAMES or suffix in {".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return RepositoryFileKind.CONFIG
    if "docs" in lower_parts or name.startswith("readme") or suffix in {".md", ".rst"}:
        return RepositoryFileKind.DOCUMENTATION
    if suffix in _SOURCE_EXTENSIONS:
        return RepositoryFileKind.SOURCE
    return RepositoryFileKind.OTHER


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, max_symbols: int) -> None:
        self.max_symbols = max_symbols
        self.symbols: list[RepositorySymbol] = []
        self.imports: list[RepositoryImport] = []
        self.scope: list[str] = []

    def _symbol(self, name: str, kind: RepositorySymbolKind, node: ast.AST) -> None:
        if len(self.symbols) >= self.max_symbols:
            return
        qualified = ".".join((*self.scope, name)) if self.scope else name
        line = int(getattr(node, "lineno", 1) or 1)
        end = int(getattr(node, "end_lineno", line) or line)
        self.symbols.append(RepositorySymbol(name, qualified, kind, line, end))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._symbol(node.name, RepositorySymbolKind.CLASS, node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = RepositorySymbolKind.METHOD if self.scope else RepositorySymbolKind.FUNCTION
        self._symbol(node.name, kind, node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(RepositoryImport(alias.name, (), 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = tuple(alias.name for alias in node.names if alias.name != "*")
        self.imports.append(RepositoryImport(node.module or "", names, int(node.level or 0)))


def _python_metadata(content: str, max_symbols: int) -> tuple[tuple[RepositorySymbol, ...], tuple[RepositoryImport, ...], str | None]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return (), (), detail[:512]
    visitor = _PythonVisitor(max_symbols)
    visitor.visit(tree)
    symbols = tuple(sorted(visitor.symbols, key=lambda x: (x.line, x.qualified_name.casefold(), x.kind.value)))
    imports = tuple(sorted(set(visitor.imports), key=lambda x: (x.level, x.module.casefold(), tuple(n.casefold() for n in x.names))))
    return symbols, imports, None


def _module_name(path: str) -> str | None:
    p = Path(path)
    if p.suffix.lower() not in {".py", ".pyi"}:
        return None
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _resolve_relative(current_module: str | None, imp: RepositoryImport) -> str:
    if imp.level == 0:
        return imp.module
    if not current_module:
        return imp.module
    package = current_module.split(".")[:-1]
    trim = max(imp.level - 1, 0)
    if trim:
        package = package[:-trim] if trim <= len(package) else []
    parts = package + ([imp.module] if imp.module else [])
    return ".".join(x for x in parts if x)


def build_relationships(files: tuple[RepositoryFile, ...]) -> tuple[tuple[RepositoryDependencyEdge, ...], tuple[RepositoryTestAssociation, ...]]:
    module_to_path: dict[str, str] = {}
    for file in files:
        module = _module_name(file.path)
        if module:
            module_to_path.setdefault(module, file.path)

    edges: set[tuple[str, str, str]] = set()
    for file in files:
        current_module = _module_name(file.path)
        for imp in file.imports:
            base = _resolve_relative(current_module, imp)
            candidates: list[tuple[str, str]] = []
            if base:
                candidates.append((base, f"import:{base}"))
                for name in imp.names:
                    candidates.append((f"{base}.{name}", f"import:{base}.{name}"))
            else:
                for name in imp.names:
                    candidates.append((name, f"import:{name}"))
            target = None
            reason = None
            for module, why in candidates:
                if module in module_to_path:
                    target, reason = module_to_path[module], why
                    break
            if target and target != file.path:
                edges.add((file.path, target, reason or "import"))

    file_map = {file.path: file for file in files}
    associations: set[tuple[str, str, str]] = set()
    for source, target, reason in edges:
        if file_map[source].kind is RepositoryFileKind.TEST and file_map[target].kind is not RepositoryFileKind.TEST:
            associations.add((source, target, f"dependency:{reason}"))

    by_stem: dict[str, list[str]] = {}
    for file in files:
        if file.kind is RepositoryFileKind.TEST:
            continue
        stem = Path(file.path).stem.lower()
        by_stem.setdefault(stem, []).append(file.path)
    for file in files:
        if file.kind is not RepositoryFileKind.TEST:
            continue
        stem = Path(file.path).stem.lower()
        if stem.startswith("test_"):
            stem = stem[5:]
        elif stem.endswith("_test"):
            stem = stem[:-5]
        for target in sorted(by_stem.get(stem, ()), key=str.casefold):
            associations.add((file.path, target, "stem_match"))

    edge_values = tuple(RepositoryDependencyEdge(*x) for x in sorted(edges, key=lambda x: (x[0].casefold(), x[1].casefold(), x[2].casefold())))
    assoc_values = tuple(RepositoryTestAssociation(*x) for x in sorted(associations, key=lambda x: (x[0].casefold(), x[1].casefold(), x[2].casefold())))
    return edge_values, assoc_values


class RepositoryIndexer:
    'Build a deterministic metadata index over one active worker agent.5 worktree.'

    def __init__(self, *, manager: GitWorktreeManager, policy: RepositoryIntelligencePolicy | None = None) -> None:
        self.manager = manager
        self.policy = policy or RepositoryIntelligencePolicy()

    def build(self, lease_id: str) -> RepositoryIndex:
        lease = self.manager.get_active(lease_id)
        root = Path(lease.worktree_root)
        before = self.manager.snapshot(lease_id)
        files: list[RepositoryFile] = []
        skipped: list[str] = []
        total_bytes = 0

        for path in self._paths(root):
            rel = path.relative_to(root).as_posix()
            try:
                if path.is_symlink():
                    skipped.append(f"{rel}:symlink")
                    continue
                size = path.stat().st_size
            except OSError as exc:
                raise RepositoryIntegrityError(f"cannot stat repository path {rel}: {exc}") from exc
            if size > self.policy.max_file_bytes:
                skipped.append(f"{rel}:oversize")
                continue
            if len(files) >= self.policy.max_files:
                raise RepositoryBudgetError("repository file count exceeds max_files")
            if total_bytes + size > self.policy.max_total_bytes:
                raise RepositoryBudgetError("repository text bytes exceed max_total_bytes")
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise RepositoryIntegrityError(f"cannot read repository path {rel}: {exc}") from exc
            if b"\x00" in payload[:8192]:
                skipped.append(f"{rel}:binary")
                continue
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{rel}:non_utf8")
                continue
            total_bytes += len(payload)
            symbols: tuple[RepositorySymbol, ...] = ()
            imports: tuple[RepositoryImport, ...] = ()
            parse_error = None
            if path.suffix.lower() in {".py", ".pyi"}:
                symbols, imports, parse_error = _python_metadata(content, self.policy.max_symbols_per_file)
            files.append(RepositoryFile(
                path=rel,
                kind=_kind(rel),
                language=_language(rel),
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                symbols=symbols,
                imports=imports,
                parse_error=parse_error,
            ))

        files_tuple = tuple(sorted(files, key=lambda x: x.path.casefold()))
        edges, associations = build_relationships(files_tuple)
        after = self.manager.snapshot(lease_id)
        if (before.head_commit, before.diff_sha256, before.dirty_paths) != (after.head_commit, after.diff_sha256, after.dirty_paths):
            raise RepositorySnapshotMismatchError("candidate changed while repository index was being built")
        return RepositoryIndex.create(
            lease_id=lease_id,
            repo_root=str(root),
            head_commit=before.head_commit,
            diff_sha256=before.diff_sha256,
            dirty_paths=before.dirty_paths,
            files=files_tuple,
            dependency_edges=edges,
            test_associations=associations,
            skipped_paths=tuple(sorted(skipped, key=str.casefold)),
        )

    def _paths(self, root: Path):
        excluded = {item.casefold() for item in self.policy.excluded_directories}
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted([d for d in dirs if d.casefold() not in excluded and not (current_path / d).is_symlink()], key=str.casefold)
            for name in sorted(names, key=str.casefold):
                if name.casefold() in excluded:
                    continue
                yield current_path / name
