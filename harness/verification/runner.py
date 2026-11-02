'Deterministic trusted gate execution inside an ACTIVE worker agent.5 worktree.'
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from ..control_plane.models import WorkContract
from ..worktree.manager import GitWorktreeManager
from ..worktree.models import WorktreeSnapshot
from .errors import VerificationIntegrityError, VerificationPolicyError, VerificationStateError
from .models import (
    BuiltinGate,
    FileAssertionMode,
    GateExecutionResult,
    GateOutcome,
    VerificationGateKind,
    VerificationGateSpec,
    VerificationGateType,
    VerificationOutcome,
    VerificationPlan,
    VerificationReport,
    GateProvenance, VerificationSubject, ExecutionPurpose, source_revision_identity,
)
from .policy import VerificationPolicy
from .regression import python_test_functions


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _scope_match(path: str, prefixes: tuple[str, ...]) -> bool:
    if not prefixes:
        return True
    value = path.replace("\\", "/").rstrip("/")
    for prefix in prefixes:
        normalized = prefix.replace("\\", "/").strip("/")
        if value == normalized or value.startswith(normalized + "/"):
            return True
    return False


class VerificationRunner:
    def __init__(self, *, manager: GitWorktreeManager, policy: VerificationPolicy, command_observer=None) -> None:
        self.manager = manager
        self.policy = policy
        self.command_observer = command_observer
        self._trusted_python_cache: str | None = None

    def run(self, *, plan: VerificationPlan, contract: WorkContract) -> VerificationReport:
        lease = self.manager.get_active(plan.lease_id)
        source_identity = self.manager.guard.assert_target_allowed(
            lease.source_repo_root, protect_same_git_common_dir=self.manager.policy.protect_same_git_common_dir
        )
        if source_identity.git_common_dir != lease.source_git_common_dir:
            raise VerificationStateError("active lease source Git identity mismatch")
        if lease.run_id != plan.run_id or lease.todo_id != plan.todo_id:
            raise VerificationStateError("verification plan and active lease identity mismatch")
        if contract.todo_id != plan.todo_id or contract.goal_id == "":
            raise VerificationStateError("verification plan and WorkContract identity mismatch")
        if len(plan.gates) > self.policy.max_gates:
            raise VerificationPolicyError("plan exceeds max_gates")
        root = Path(lease.worktree_root).resolve(strict=True)
        before = self.manager.snapshot(lease.lease_id)
        if self.policy.require_candidate_dirty and not before.dirty:
            raise VerificationStateError("candidate worktree must contain a dirty change before verification")

        started_at = _utc_now()
        started = time.monotonic()
        results: list[GateExecutionResult] = []
        stop = False
        for gate in plan.gates:
            if stop:
                results.append(self._not_run(gate, "not run after required gate failure"))
                continue
            remaining = self.policy.max_total_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                results.append(self._not_run(gate, "verification total timeout exhausted"))
                stop = True
                continue
            result = self._execute_gate(gate, contract, root, before, max_seconds=remaining)
            results.append(result)
            if gate.kind is VerificationGateKind.COMMAND:
                current = self.manager.snapshot(lease.lease_id)
                if (
                    current.head_commit != before.head_commit
                    or current.diff_sha256 != before.diff_sha256
                    or current.dirty_paths != before.dirty_paths
                    or getattr(current, 'content_sha256', None) != getattr(before, 'content_sha256', None)
                ):
                    results.append(GateExecutionResult(
                        gate_id="verification_candidate_immutability",
                        gate_type=VerificationGateType.CUSTOM,
                        required=True,
                        outcome=GateOutcome.ERROR,
                        duration_ms=0,
                        detail=f"verification command {gate.gate_id} mutated candidate worktree",
                    ))
                    stop = True
                    continue
            if self.policy.fail_fast and gate.required and result.outcome is not GateOutcome.PASSED:
                stop = True

        after = self.manager.snapshot(lease.lease_id)
        if (
            after.head_commit != before.head_commit
            or after.diff_sha256 != before.diff_sha256
            or after.dirty_paths != before.dirty_paths
            or getattr(after, 'content_sha256', None) != getattr(before, 'content_sha256', None)
        ) and not any(item.gate_id == "verification_candidate_immutability" for item in results):
            # Commands used as verification gates are not allowed to alter the candidate being judged.
            mutation = GateExecutionResult(
                gate_id="verification_candidate_immutability",
                gate_type=VerificationGateType.CUSTOM,
                required=True,
                outcome=GateOutcome.ERROR,
                duration_ms=0,
                detail="verification execution mutated candidate worktree",
            )
            results.append(mutation)

        outcome = VerificationOutcome.PASSED if all(
            (not item.required) or item.outcome is GateOutcome.PASSED for item in results
        ) else VerificationOutcome.FAILED
        report_id = self.report_id(plan, before)
        # Bind observed process results to the actual tested revision. A raw
        # assertion failure on original SOURCE is not a candidate failure.
        specs = {gate.gate_id: gate for gate in plan.gates}
        bound = []
        for result in results:
            spec = specs.get(result.gate_id)
            builtin = spec.builtin if spec is not None and spec.kind is VerificationGateKind.BUILTIN else None
            negative = builtin is BuiltinGate.REGRESSION_ORACLE
            purpose = (ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL if negative else
                       ExecutionPurpose.IMMUTABLE_BASELINE if builtin is BuiltinGate.IMMUTABLE_BASELINE_TESTS else
                       ExecutionPurpose.CANDIDATE_VALIDATION)
            identity = source_revision_identity(lease.base_commit, hashlib.sha256(b"").hexdigest()) if negative else source_revision_identity(before.head_commit, before.diff_sha256)
            bound.append(replace(result, provenance=GateProvenance(
                VerificationSubject.ORIGINAL_SOURCE if negative else VerificationSubject.CANDIDATE,
                purpose, result.exit_code, result.outcome, negative, report_id, identity)))
        return VerificationReport(
            report_id=report_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            run_id=plan.run_id,
            todo_id=plan.todo_id,
            lease_id=plan.lease_id,
            candidate_head=before.head_commit,
            candidate_diff_sha256=before.diff_sha256,
            candidate_content_sha256=getattr(before, 'content_sha256', None), candidate_file_sha256=getattr(before, 'file_sha256', ()),
            dirty_paths=before.dirty_paths,
            started_at=started_at,
            completed_at=_utc_now(),
            results=tuple(bound),
            outcome=outcome,
        )

    @staticmethod
    def report_id(plan: VerificationPlan, snapshot: WorktreeSnapshot) -> str:
        digest = hashlib.sha256(
            f"{plan.run_id}\0{plan.todo_id}\0{plan.lease_id}\0{plan.fingerprint}\0{snapshot.head_commit}\0{snapshot.diff_sha256}\0{getattr(snapshot, 'content_sha256', None)}".encode("utf-8")
        ).hexdigest()[:20]
        return f"{plan.run_id}:verification:{digest}"

    def _execute_gate(
        self,
        gate: VerificationGateSpec,
        contract: WorkContract,
        root: Path,
        snapshot: WorktreeSnapshot,
        *,
        max_seconds: float,
    ) -> GateExecutionResult:
        started = time.monotonic()
        try:
            if gate.kind is VerificationGateKind.BUILTIN:
                if gate.builtin in (BuiltinGate.REGRESSION_PRESERVATION,
                        BuiltinGate.IMMUTABLE_BASELINE_TESTS, BuiltinGate.REGRESSION_ORACLE):
                    from .test_contract import evaluate
                    try:
                        result = evaluate(self, gate, contract, root, snapshot, max_seconds)
                    except subprocess.TimeoutExpired:
                        result = GateExecutionResult(gate.gate_id, gate.gate_type, gate.required,
                            GateOutcome.TIMEOUT, 0, 'isolated test contract timeout')
                    except (OSError, ValueError, SyntaxError) as exc:
                        raise VerificationStateError(f'test contract evidence invalid: {exc}') from exc
                else:
                    result = self._builtin(gate, contract, root, snapshot)
            elif gate.kind is VerificationGateKind.FILE_ASSERTION:
                result = self._file_assertion(gate, root)
            elif gate.kind is VerificationGateKind.COMMAND:
                result = self._command(gate, root, max_seconds=max_seconds)
            else:
                raise VerificationPolicyError(f"unsupported gate kind: {gate.kind}")
        except (VerificationPolicyError, VerificationStateError, VerificationIntegrityError) as exc:
            result = GateExecutionResult(
                gate_id=gate.gate_id,
                gate_type=gate.gate_type,
                required=gate.required,
                outcome=GateOutcome.ERROR,
                duration_ms=0,
                detail=f"{type(exc).__name__}: {exc}",
            )
        duration = max(0, int((time.monotonic() - started) * 1000))
        if result.duration_ms == duration:
            return result
        return GateExecutionResult(
            gate_id=result.gate_id,
            gate_type=result.gate_type,
            required=result.required,
            outcome=result.outcome,
            duration_ms=duration,
            detail=result.detail,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
        )

    def _builtin(
        self,
        gate: VerificationGateSpec,
        contract: WorkContract,
        root: Path,
        snapshot: WorktreeSnapshot,
    ) -> GateExecutionResult:
        if gate.builtin is BuiltinGate.CANDIDATE_DIFF:
            ok = snapshot.dirty
            return self._simple(gate, ok, f"dirty_paths={len(snapshot.dirty_paths)}")
        if gate.builtin is BuiltinGate.SCOPE_GUARD:
            invalid: list[str] = []
            for path in snapshot.dirty_paths:
                if not _scope_match(path, tuple(contract.allowed_scope)):
                    invalid.append(path)
                    continue
                if contract.forbidden_scope and _scope_match(path, tuple(contract.forbidden_scope)):
                    invalid.append(path)
            return self._simple(gate, not invalid, "scope valid" if not invalid else f"out-of-scope paths: {invalid}")
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
                    source = candidate.read_text(encoding="utf-8")
                    compile(source, rel, "exec", dont_inherit=True)
                    checked += 1
                except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                    failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            return self._simple(
                gate,
                not failures,
                f"python_files_checked={checked}" if not failures else "; ".join(failures),
            )
        if gate.builtin is BuiltinGate.REGRESSION_CHANGE:
            # Reuse repository classification, not a second test-path policy.
            from ..repo_intelligence.scanner import _kind
            from ..repo_intelligence.models import RepositoryFileKind
            changed = []
            for rel in snapshot.dirty_paths:
                if (_kind(rel) is not RepositoryFileKind.TEST or Path(rel).suffix.lower() != ".py"
                        or not _scope_match(rel, tuple(contract.allowed_scope))
                        or contract.forbidden_scope and _scope_match(rel, tuple(contract.forbidden_scope))):
                    continue
                candidate = self._safe_path(root, rel, allow_missing=True)
                if not candidate.is_file() or candidate.stat().st_size > self.policy.max_source_bytes:
                    continue
                try:
                    current = python_test_functions(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                    return self._simple(gate, False, f"{rel}: {type(exc).__name__}: {exc}")
                if not current:
                    continue
                from ..worktree.errors import GitRepositoryError
                try:
                    listing = self.manager.git.run(root, "ls-tree", "-z", snapshot.head_commit, "--", rel).stdout
                    previous = {}
                    if listing:
                        blob = listing.split("\t", 1)[0].split()[2]
                        size = int(self.manager.git.text(root, "cat-file", "-s", blob))
                        if size > self.policy.max_source_bytes:
                            continue
                        original = self.manager.git.run(root, "cat-file", "blob", blob).stdout
                        previous = python_test_functions(original)
                except (GitRepositoryError, UnicodeError, SyntaxError, ValueError, IndexError) as exc:
                    raise VerificationStateError(f"cannot compare regression baseline for {rel}: {exc}") from exc
                if any(previous.get(name) != body for name, body in current.items()):
                    changed.append(rel)
            return self._simple(gate, bool(changed),
                f"changed_python_test_functions={changed}" if changed else
                "REGRESSION_CHANGE_REQUIRED: add or update a Python test function; source-only, deleted tests and comment/docstring-only changes are insufficient")
        raise VerificationPolicyError(f"unknown builtin gate: {gate.builtin}")

    def _file_assertion(self, gate: VerificationGateSpec, root: Path) -> GateExecutionResult:
        assert gate.path is not None and gate.assertion_mode is not None
        candidate = self._safe_path(root, gate.path, allow_missing=True)
        mode = gate.assertion_mode
        if mode is FileAssertionMode.EXISTS:
            return self._simple(gate, candidate.is_file(), "file exists" if candidate.is_file() else "file missing")
        if mode is FileAssertionMode.ABSENT:
            return self._simple(gate, not candidate.exists(), "path absent" if not candidate.exists() else "path exists")
        if not candidate.is_file():
            return self._simple(gate, False, "assertion target is not a regular file")
        if candidate.stat().st_size > self.policy.max_source_bytes:
            return self._simple(gate, False, "assertion target exceeds max_source_bytes")
        if mode is FileAssertionMode.SHA256:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            return self._simple(gate, actual == gate.expected, f"sha256={actual}")
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return self._simple(gate, False, f"cannot read UTF-8 target: {exc}")
        if mode is FileAssertionMode.EXACT_TEXT:
            return self._simple(gate, content == gate.expected, "exact text matched" if content == gate.expected else f"exact text mismatch actual={content!r}")
        if mode is FileAssertionMode.CONTAINS_TEXT:
            assert gate.expected is not None
            return self._simple(gate, gate.expected in content, "text contained" if gate.expected in content else "expected text not found")
        raise VerificationPolicyError(f"unsupported assertion mode: {mode}")

    def _command(self, gate: VerificationGateSpec, root: Path, *, max_seconds: float) -> GateExecutionResult:
        assert gate.catalog_id is not None
        entry = self.policy.command_catalog.get(gate.catalog_id)
        if entry is None:
            raise VerificationPolicyError(f"command catalog entry missing at execution: {gate.catalog_id}")
        if entry.gate_type is not gate.gate_type:
            raise VerificationIntegrityError("gate type no longer matches trusted command catalog")
        env = self._minimal_env(root=root)
        argv = self._resolve_argv(entry.argv, root=root, env=env)
        timeout = max(0.01, min(float(entry.timeout_seconds), float(self.policy.max_gate_timeout_seconds), max_seconds))
        creationflags, popen_kwargs = self._process_launch_options()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    shell=False,
                    creationflags=creationflags,
                    **popen_kwargs,
                )
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                detail = f"process start failed: argv0={argv[0]!r}; winerror={winerror!r}; {exc}"
                return GateExecutionResult(gate.gate_id, gate.gate_type, gate.required, GateOutcome.ERROR, 0, detail)
            timed_out = False
            try:
                if self.command_observer is not None:
                    self.command_observer('STARTED', dict(gate_id=gate.gate_id, pid=process.pid,
                        parent_pid=os.getpid(), argv=argv, cwd=str(root), timeout_seconds=timeout))
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_tree(process)
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=5)
            except BaseException:
                # A durable observer failure must not leave an unowned verifier.
                self._terminate_tree(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise
            stdout, trunc_out = self._read_temp(out)
            stderr, trunc_err = self._read_temp(err)
        if self.command_observer is not None:
            self.command_observer('FINISHED', dict(gate_id=gate.gate_id, pid=process.pid,
                parent_pid=os.getpid(), argv=argv, cwd=str(root), timeout_seconds=timeout,
                exit_code=exit_code, timed_out=timed_out, stdout_sha256=hashlib.sha256(stdout.encode('utf8')).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr.encode('utf8')).hexdigest(), output_truncated=trunc_out or trunc_err))
        if timed_out:
            return GateExecutionResult(
                gate.gate_id, gate.gate_type, gate.required, GateOutcome.TIMEOUT, 0,
                f"command exceeded timeout_seconds={timeout:g}", exit_code, stdout, stderr, trunc_out or trunc_err,
            )
        passed = exit_code in entry.expected_exit_codes
        return GateExecutionResult(
            gate.gate_id, gate.gate_type, gate.required,
            GateOutcome.PASSED if passed else GateOutcome.FAILED,
            0,
            f"exit_code={exit_code}; expected={entry.expected_exit_codes}",
            exit_code,
            stdout,
            stderr,
            trunc_out or trunc_err,
        )

    def _resolve_argv(self, argv: tuple[str, ...], *, root: Path, env: dict[str, str] | None = None) -> list[str]:
        first = argv[0]
        if first == "{python}":
            executable = self._trusted_python_executable(root=root, env=env)
            return [executable, *argv[1:]]
        if first == "{git}":
            found = shutil.which("git")
            if found is None:
                raise VerificationPolicyError("trusted git executable not found")
            executable = str(Path(found).resolve(strict=True))
        else:
            executable = str(Path(first).expanduser().resolve(strict=True))
        return [executable, *argv[1:]]

    def _trusted_python_executable(self, *, root: Path, env: dict[str, str] | None) -> str:
        "Return the single harness-owned Python executable for command gates.\n\n        verification never performs a PATH search for Python.  On Windows, ``Popen``\n        with ``shell=False`` cannot use the child ``env['PATH']`` to control\n        executable lookup, so a bare ``python``/``py`` command would violate the\n        trusted-command boundary.  The configured project virtual-environment\n        interpreter is therefore required to be an absolute path and is probed\n        directly before first use.\n        "
        if self._trusted_python_cache is not None:
            return self._trusted_python_cache

        raw = self.policy.trusted_python_executable
        if raw is None:
            raise VerificationPolicyError(
                "trusted Python runtime is not configured; run scripts/setup_verification_runtime.ps1 "
                "and configure verification.runtime.trusted_python_venv"
            )
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise VerificationPolicyError("trusted Python executable must be an absolute path")

        # IMPORTANT (Windows): do not call Path.resolve()/os.path.realpath() on the
        # configured venv launcher.  Store-derived virtual environments can be
        # backed by WindowsApps file-system redirections whose CreateFile/realpath
        # target differs from the path that must be handed to CreateProcess.
        # verification therefore preserves the harness-owned, fully-qualified venv path
        # exactly (modulo lexical abspath/normpath) and launches that literal path.
        executable = Path(os.path.abspath(os.path.normpath(str(candidate))))
        if not executable.is_file():
            raise VerificationPolicyError(f"trusted Python executable is not a regular file: {executable}")

        root_absolute = Path(os.path.abspath(os.path.normpath(str(root))))
        if _within(executable, root_absolute):
            raise VerificationPolicyError("trusted Python executable must not be inside candidate worktree")
        if os.name == "nt" and "windowsapps" in {part.casefold() for part in executable.parts}:
            raise VerificationPolicyError(
                "trusted Python executable must not be configured as a WindowsApps path; "
                "use the project-local .venv interpreter path literally"
            )

        probe_env = dict(env) if env is not None else self._base_minimal_env()
        creationflags, popen_kwargs = self._process_launch_options()
        try:
            completed = subprocess.run(
                [str(executable), "-I", "-S", "-c", "import sys; raise SystemExit(0)"],
                cwd=root_absolute,
                env=probe_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                check=False,
                creationflags=creationflags,
                **popen_kwargs,
            )
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            suffix = f" winerror={winerror}" if winerror is not None else ""
            raise VerificationPolicyError(
                f"trusted Python executable cannot start:{suffix} {executable}: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VerificationPolicyError(f"trusted Python executable probe timed out: {executable}") from exc
        if completed.returncode != 0:
            raise VerificationPolicyError(
                f"trusted Python executable probe failed with exit_code={completed.returncode}: {executable}"
            )

        self._trusted_python_cache = str(executable)
        return self._trusted_python_cache

    @staticmethod
    def _process_launch_options() -> tuple[int, dict[str, object]]:
        if os.name == "nt":
            return 0, {}
        return 0, {"start_new_session": True}

    @staticmethod
    def _base_minimal_env() -> dict[str, str]:
        # SYSTEMDRIVE and PROGRAMDATA are part of the trusted Windows runtime
        # environment. Store/MSIX-backed Python may expand system cache paths
        # through these variables during interpreter startup. Dropping them can
        # leave a literal %SystemDrive% path relative to the candidate worktree,
        # which is then correctly detected as an unexpected candidate mutation.
        allowed = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PROGRAMDATA", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "PATHEXT")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            "CI": "1",
        })
        return env

    def _minimal_env(self, *, root: Path | None = None) -> dict[str, str]:
        env = self._base_minimal_env()
        if root is None or "PATH" not in env:
            return env

        # Keep only absolute PATH entries and remove the candidate worktree (or
        # its descendants).  This prevents a repository from becoming an
        # executable source through inherited relative PATH components.
        cleaned: list[str] = []
        for raw in env["PATH"].split(os.pathsep):
            item = raw.strip().strip('"')
            if not item:
                continue
            try:
                path = Path(item).expanduser()
                if not path.is_absolute():
                    continue
                normalized = path.resolve(strict=False)
                if _within(normalized, root):
                    continue
            except (OSError, RuntimeError):
                continue
            cleaned.append(str(path))
        env["PATH"] = os.pathsep.join(cleaned)
        return env

    def _read_temp(self, handle) -> tuple[str, bool]:
        handle.flush()
        handle.seek(0)
        payload = handle.read(self.policy.max_output_bytes + 1)
        truncated = len(payload) > self.policy.max_output_bytes
        payload = payload[: self.policy.max_output_bytes]
        return payload.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=10,
                    check=False,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()

    def _safe_path(self, root: Path, relative: str, *, allow_missing: bool) -> Path:
        raw = root / relative
        try:
            if raw.is_symlink():
                raise VerificationStateError(f"verification path is a symlink: {relative}")
            resolved = raw.resolve(strict=not allow_missing)
        except OSError as exc:
            raise VerificationStateError(f"cannot resolve verification path {relative}: {exc}") from exc
        if not _within(resolved, root):
            raise VerificationStateError(f"verification path escapes worktree: {relative}")
        return resolved

    @staticmethod
    def _simple(gate: VerificationGateSpec, passed: bool, detail: str) -> GateExecutionResult:
        return GateExecutionResult(
            gate.gate_id,
            gate.gate_type,
            gate.required,
            GateOutcome.PASSED if passed else GateOutcome.FAILED,
            0,
            detail,
        )

    @staticmethod
    def _not_run(gate: VerificationGateSpec, detail: str) -> GateExecutionResult:
        return GateExecutionResult(gate.gate_id, gate.gate_type, gate.required, GateOutcome.NOT_RUN, 0, detail)
