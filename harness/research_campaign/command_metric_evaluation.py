'research diagnostics.1 Generic Subprocess Command Metric Evaluation Port.'
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .contracts import EvaluationContractV1
from .models import ExperimentFailureKindV1, ExperimentSpecV1
from .ports import (
    ExperimentEvaluationStatusV1,
    ExperimentEvaluationV1,
    ExperimentExecutionOutcomeV1,
    ExperimentExecutionStatusV1,
)
from .profile import CommandMetricEvaluationConfigV1, compute_file_sha256
from .serialization import (
    finite_number,
    freeze_json,
    git_commit,
    plain,
    text,
)


def _git_eval_cmd(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd} (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


class _EvaluationIntegrityError(RuntimeError):
    """A command changed the candidate or left its integrity unverifiable."""


def _input_fingerprint(root: Path, relative_path: str) -> tuple[str, str | None]:
    path = root / relative_path
    return (
        compute_file_sha256(root, relative_path),
        str(path.readlink()) if path.is_symlink() else None,
    )


def _candidate_inputs(root: Path, candidate_commit: str) -> dict[str, tuple[str, str | None]]:
    # Bind to the commit's file list, not a command-modifiable Git index. Reuse
    # profile fingerprints for content hashing and resolved-path containment.
    inputs = {}
    for entry in _git_eval_cmd(root, "ls-tree", "-r", "-z", candidate_commit).split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            if metadata.split()[1] == "blob":
                inputs[path] = _input_fingerprint(root, path)
    return inputs


def _assert_evaluation_integrity(
    root: Path, candidate_commit: str, inputs: dict[str, tuple[str, str | None]],
) -> None:
    head = _git_eval_cmd(root, "rev-parse", "HEAD").strip()
    if head != candidate_commit:
        raise _EvaluationIntegrityError(f"HEAD changed: expected {candidate_commit}, observed {head}")
    changed = _git_eval_cmd(
        root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
        "--ignore-submodules=none", candidate_commit, "--",
    )
    if changed:
        paths = changed.rstrip("\0").split("\0")
        raise _EvaluationIntegrityError(f"candidate changes: {paths}")
    # Byte fingerprints also catch mutations hidden by assume-unchanged /
    # skip-worktree flags or Git clean filters. New untracked scratch is allowed.
    mutated = [path for path, before in inputs.items() if _input_fingerprint(root, path) != before]
    if mutated:
        raise _EvaluationIntegrityError(f"evaluation inputs changed: {mutated}")


class CommandMetricEvaluationPortV1:
    """Evaluates candidate experiments by running explicit configured shell-less CLI commands.

    TRUST BOUNDARY:
    Commands are configured in CommandMetricEvaluationConfigV1 by trusted profile code.
    Execution occurs inside an ephemeral detached worktree pointing at candidate_commit.
    Successfully acquired worktrees are cleaned up in finally.
    """

    def __init__(
        self,
        *,
        target: Path,
        scratch_root: Path,
        python: Path,
        config: CommandMetricEvaluationConfigV1,
        allowed_mutation_scope: tuple[str, ...],
        original_baseline_commit: str,
        evaluation_contract: EvaluationContractV1,
    ) -> None:
        self.target = Path(target).resolve(strict=True)
        self.scratch_root = Path(scratch_root).resolve(strict=False)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self.python = Path(python).resolve(strict=True)
        self.config = config
        self.allowed_mutation_scope = allowed_mutation_scope
        self.original_baseline_commit = git_commit(
            original_baseline_commit, "original_baseline_commit"
        )
        self.evaluation_contract = evaluation_contract
        self.splits_called: list[str] = []
        self.records: list[dict[str, Any]] = []

    def evaluate(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
    ) -> ExperimentEvaluationV1:
        metric_calls = 0

        # 1. Enforce Evaluation Contract Identity BEFORE any worktree or command
        if spec.evaluation_contract_hash != self.evaluation_contract.contract_hash:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.INVALID,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": (
                        f"evaluation contract hash mismatch: spec {spec.evaluation_contract_hash} "
                        f"!= expected {self.evaluation_contract.contract_hash}"
                    ),
                },
                failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
            )

        if spec.dataset_fingerprint != self.evaluation_contract.dataset_fingerprint:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.INVALID,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": (
                        f"dataset fingerprint mismatch: spec {spec.dataset_fingerprint} "
                        f"!= expected {self.evaluation_contract.dataset_fingerprint}"
                    ),
                },
                failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
            )

        # 2. Check execution status
        if execution.status is not ExperimentExecutionStatusV1.COMPLETED:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.FAILED,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": f"Execution status was {execution.status.value}, not COMPLETED",
                },
                failure_kind=execution.failure_kind or ExperimentFailureKindV1.IMPLEMENTATION_BUG,
            )

        # 3. Validate candidate commit
        candidate_commit = execution.candidate_commit
        if not candidate_commit:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.FAILED,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": "No candidate commit provided in execution outcome",
                },
                failure_kind=ExperimentFailureKindV1.IMPLEMENTATION_BUG,
            )

        try:
            candidate_commit = git_commit(candidate_commit, "candidate_commit")
            candidate_commit = _git_eval_cmd(
                self.target, "rev-parse", "--verify", f"{candidate_commit}^{{commit}}"
            ).strip()
        except Exception as exc:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.FAILED,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": f"Invalid or unresolvable candidate commit '{candidate_commit}': {exc}",
                },
                failure_kind=ExperimentFailureKindV1.IMPLEMENTATION_BUG,
            )

        # 4. Check Git diff against allowed mutation scope
        changed_files: list[str] = []
        if candidate_commit != self.original_baseline_commit:
            try:
                raw_diff = _git_eval_cmd(
                    self.target,
                    "diff",
                    "--name-only",
                    f"{self.original_baseline_commit}..{candidate_commit}",
                )
                changed_files = [
                    line.strip().replace("\\", "/")
                    for line in raw_diff.splitlines()
                    if line.strip()
                ]
            except Exception as exc:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.FAILED,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "evaluation_error": f"Failed to diff candidate commit: {exc}",
                    },
                    failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
                )

            forbidden = [f for f in changed_files if f not in self.allowed_mutation_scope]
            if forbidden:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.INVALID,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "reason": f"candidate modified forbidden path(s): {forbidden}",
                        "changed_files": changed_files,
                    },
                    failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
                )

        # 5. Create ephemeral detached worktree and execute evaluation commands
        reservation = None
        worktree_added = False

        try:
            # Reserve a private container atomically; Git ownership starts only after add.
            reservation = Path(tempfile.mkdtemp(
                prefix=f"eval-{candidate_commit[:12]}-", dir=self.scratch_root,
            ))
            worktree_dir = reservation / "worktree"
            _git_eval_cmd(
                self.target,
                "worktree",
                "add",
                "--detach",
                str(worktree_dir),
                candidate_commit,
            )
            worktree_added = True

            # If expected_config_path is configured, require EXACT equality with spec.parameters
            if self.config.expected_config_path:
                cfg_file = worktree_dir / self.config.expected_config_path
                if not cfg_file.exists():
                    return ExperimentEvaluationV1(
                        status=ExperimentEvaluationStatusV1.INVALID,
                        primary_metric=None,
                        telemetry={
                            "metric_calls": metric_calls,
                            "reason": f"Expected config file missing in candidate worktree: {self.config.expected_config_path}",
                        },
                        failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
                    )
                try:
                    loaded_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                    expected_params = dict(plain(spec.parameters))
                    if loaded_cfg != expected_params:
                        return ExperimentEvaluationV1(
                            status=ExperimentEvaluationStatusV1.INVALID,
                            primary_metric=None,
                            telemetry={
                                "metric_calls": metric_calls,
                                "reason": (
                                    f"Candidate config at {self.config.expected_config_path} does not exactly match "
                                    f"spec parameters: {loaded_cfg} != {expected_params}"
                                ),
                            },
                            failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
                        )
                except Exception as exc:
                    return ExperimentEvaluationV1(
                        status=ExperimentEvaluationStatusV1.INVALID,
                        primary_metric=None,
                        telemetry={
                            "metric_calls": metric_calls,
                            "reason": f"Failed to parse expected config: {exc}",
                        },
                        failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
                    )

            inputs = _candidate_inputs(worktree_dir, candidate_commit)
            if self.config.expected_config_path:
                inputs[self.config.expected_config_path] = _input_fingerprint(
                    worktree_dir, self.config.expected_config_path,
                )

            def run_checked_command(argv: Sequence[str], stage: str) -> subprocess.CompletedProcess[str]:
                try:
                    return subprocess.run(
                        argv, cwd=str(worktree_dir), capture_output=True, text=True,
                        timeout=self.config.timeout_seconds, check=False,
                    )
                finally:
                    # Check even after nonzero exits/timeouts. Never repair the
                    # worktree and then attribute its metric to the old candidate.
                    try:
                        _assert_evaluation_integrity(worktree_dir, candidate_commit, inputs)
                    except Exception as exc:
                        raise _EvaluationIntegrityError(f"{stage}: {exc}") from exc

            # Verification command (if present)
            if self.config.verification_argv:
                verif_cmd = [
                    str(self.python) if arg == "{python}" else arg
                    for arg in self.config.verification_argv
                ]
                proc_verif = run_checked_command(verif_cmd, "after_verification")
                if proc_verif.returncode != 0:
                    return ExperimentEvaluationV1(
                        status=ExperimentEvaluationStatusV1.FAILED,
                        primary_metric=None,
                        telemetry={
                            "metric_calls": metric_calls,
                            "evaluation_error": f"Verification command failed with exit code {proc_verif.returncode}",
                            "stdout": proc_verif.stdout[:4000],
                            "stderr": proc_verif.stderr[:4000],
                        },
                        failure_kind=ExperimentFailureKindV1.IMPLEMENTATION_BUG,
                    )

            # Evaluation command - set metric_calls = 1 immediately before execution
            eval_cmd = [
                str(self.python) if arg == "{python}" else arg
                for arg in self.config.evaluation_argv
            ]
            self.splits_called.append(self.config.research_split)
            metric_calls = 1
            proc_eval = run_checked_command(eval_cmd, "after_evaluation")
            if proc_eval.returncode != 0:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.FAILED,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "evaluation_error": f"Evaluation command failed with exit code {proc_eval.returncode}",
                        "stdout": proc_eval.stdout[:4000],
                        "stderr": proc_eval.stderr[:4000],
                    },
                    failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
                )

            # Parse stdout JSON
            try:
                eval_out = json.loads(proc_eval.stdout.strip())
            except Exception as exc:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.FAILED,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "evaluation_error": f"Failed to parse JSON evaluation stdout: {exc}",
                        "stdout": proc_eval.stdout[:4000],
                    },
                    failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
                )

            if not isinstance(eval_out, dict) or self.config.metric_json_key not in eval_out:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.FAILED,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "evaluation_error": f"Expected metric key '{self.config.metric_json_key}' not found in output: {eval_out}",
                    },
                    failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
                )

            raw_metric = eval_out[self.config.metric_json_key]
            try:
                metric_val = finite_number(float(raw_metric), "metric")
            except Exception as exc:
                return ExperimentEvaluationV1(
                    status=ExperimentEvaluationStatusV1.FAILED,
                    primary_metric=None,
                    telemetry={
                        "metric_calls": metric_calls,
                        "evaluation_error": f"Metric value is not a finite number: {exc}",
                    },
                    failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
                )

            record = {
                "experiment_id": spec.experiment_id,
                "candidate_commit": candidate_commit,
                "metric": metric_val,
                "parameters": dict(plain(spec.parameters)),
                "changed_paths_from_original_baseline": changed_files,
                "split": self.config.research_split,
            }
            self.records.append(record)

            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.SCORED,
                primary_metric=metric_val,
                telemetry={
                    "metric_calls": metric_calls,
                    "split": self.config.research_split,
                    "candidate_commit": candidate_commit,
                },
            )

        except _EvaluationIntegrityError as exc:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.INVALID,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "candidate_commit": candidate_commit,
                    "evaluation_integrity": "FAILED",
                    "evaluation_error": str(exc),
                },
                failure_kind=ExperimentFailureKindV1.INVALID_METRIC,
            )
        except subprocess.TimeoutExpired as exc:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.FAILED,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": f"Command timed out after {exc.timeout}s",
                },
                failure_kind=ExperimentFailureKindV1.TIMEOUT,
            )
        except Exception as exc:
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.FAILED,
                primary_metric=None,
                telemetry={
                    "metric_calls": metric_calls,
                    "evaluation_error": f"Evaluation error: {exc}",
                },
                failure_kind=ExperimentFailureKindV1.EVALUATION_FAILED,
            )
        finally:
            if worktree_added:
                proc_rm = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.target),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree_dir),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                # Verify exact worktree path no longer in git worktree list --porcelain
                proc_list = subprocess.run(
                    ["git", "-C", str(self.target), "worktree", "list", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                target_wt_norm = str(worktree_dir.resolve()).replace("\\", "/").lower()
                ledger_worktrees = [
                    line[9:].strip().replace("\\", "/").lower()
                    for line in proc_list.stdout.splitlines()
                    if line.startswith("worktree ")
                ]
                if proc_list.returncode != 0 or target_wt_norm in ledger_worktrees:
                    raise RuntimeError(
                        f"Failed to remove ephemeral evaluator worktree '{worktree_dir}' from Git ledger (rc={proc_rm.returncode}): {proc_rm.stderr.strip()}"
                    )
                if worktree_dir.exists():
                    shutil.rmtree(worktree_dir)
            if reservation is not None:
                # Failed add does not authorize recursive deletion of its target.
                # Remove only our empty reservation; retain any partial state.
                try:
                    reservation.rmdir()
                except OSError:
                    if worktree_added or not reservation.is_dir() or not any(reservation.iterdir()):
                        raise
