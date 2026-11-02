"""Production verification runtime with native Python binary source parsing."""
from __future__ import annotations

from pathlib import Path

from ..control_plane.models import WorkContract
from ..verification.models import BuiltinGate, GateExecutionResult, VerificationGateSpec
from ..verification.runner import VerificationRunner
from ..worktree.models import WorktreeSnapshot


class ProductionVerificationRunner(VerificationRunner):
    """Additive production verification runner.

    Preserves exact frozen VerificationRunner behavior for all gates,
    but parses PYTHON_SYNTAX candidate sources via compile(candidate.read_bytes())
    so that native Python encoding tokenization (UTF-8 BOM, PEP 263 headers)
    is handled without incompatibility.
    """

    def _builtin(
        self,
        gate: VerificationGateSpec,
        contract: WorkContract,
        root: Path,
        snapshot: WorktreeSnapshot,
    ) -> GateExecutionResult:
        if gate.builtin is BuiltinGate.PYTHON_SYNTAX:
            checked = 0
            failures: list[str] = []
            for rel in snapshot.dirty_paths:
                if not rel.lower().endswith(".py"):
                    continue
                candidate = self._safe_path(root, rel, allow_missing=True)
                if not candidate.exists():
                    continue
                if candidate.stat().st_size > self.policy.max_source_bytes:
                    failures.append(f"{rel}: exceeds max_source_bytes")
                    continue
                try:
                    source = candidate.read_bytes()
                    compile(source, rel, "exec", dont_inherit=True)
                    checked += 1
                except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                    failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            return self._simple(
                gate,
                not failures,
                f"python_files_checked={checked}" if not failures else "; ".join(failures),
            )
        return super()._builtin(gate, contract, root, snapshot)
