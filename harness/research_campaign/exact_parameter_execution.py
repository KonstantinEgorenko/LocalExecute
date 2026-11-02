'Trusted deterministic materialization for exact parameter JSON experiments.\n\nThis module is deliberately narrow. It does not replace the ResearchCampaign\ncontroller, mission/workflow, verification, or evaluation. It only bypasses LLM\nexecution when a trusted profile and an ExperimentSpec together prove that the\nentire requested mutation is an authoritative JSON parameter mapping for one\nexplicitly allowed file.\n\nThe existing CommandMetricEvaluationPortV1 remains authoritative for mutation\nscope, exact JSON equality, trusted verification commands, and DEV scoring.\nAll ineligible experiments delegate unchanged to the existing production Swarm\nexecution port.\n'
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..worktree.git import GitClient
from .models import ExperimentFailureKindV1, ExperimentSpecV1
from .ports import (
    ExperimentExecutionOutcomeV1,
    ExperimentExecutionPort,
    ExperimentExecutionStatusV1,
)
from .search import StrategyDimensionTargetV1
from .serialization import canonical_json, plain, stable_sha256


class ExactParameterMaterializationError(RuntimeError):
    """Fail-closed deterministic materialization error."""


@dataclass(frozen=True, slots=True)
class ExactParameterMaterializationPolicyV1:
    target_path: str
    parameter_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_path, str) or not self.target_path.strip():
            raise ValueError("target_path must be non-empty string")
        target = self.target_path.strip().replace("\\", "/")
        parts = target.split("/")
        if (
            target.startswith("/")
            or ":" in target
            or any(part in {"", ".", ".."} for part in parts)
            or Path(target).suffix.casefold() != ".json"
        ):
            raise ValueError("target_path must be a normalized relative JSON path")
        object.__setattr__(self, "target_path", target)

        if (
            not isinstance(self.parameter_keys, tuple)
            or not self.parameter_keys
            or any(not isinstance(item, str) or not item for item in self.parameter_keys)
        ):
            raise ValueError("parameter_keys must be a non-empty tuple of strings")
        if tuple(sorted(set(self.parameter_keys))) != self.parameter_keys:
            raise ValueError("parameter_keys must be unique and sorted")


def exact_parameter_policy_from_profile(
    profile: Any,
) -> ExactParameterMaterializationPolicyV1 | None:
    """Derive eligibility only from an already trusted code-constructed profile."""
    config = getattr(profile, "evaluation_config", None)
    target = getattr(config, "expected_config_path", None)
    if not isinstance(target, str) or not target:
        return None
    normalized_target = target.replace("\\", "/")
    if Path(normalized_target).suffix.casefold() != ".json":
        return None

    allowed = tuple(getattr(profile, "allowed_mutation_scope", ()))
    if allowed != (normalized_target,):
        return None

    constraints = getattr(profile, "execution_global_constraints", {})
    if not isinstance(constraints, Mapping):
        return None
    if constraints.get("selected_parameters_are_authoritative") is not True:
        return None

    dimensions = tuple(
        getattr(getattr(profile, "strategy_space", None), "dimensions", ())
    )
    if not dimensions:
        return None
    if any(
        getattr(item, "target", None) is not StrategyDimensionTargetV1.PARAMETER
        for item in dimensions
    ):
        return None

    raw_keys = tuple(getattr(item, "key", None) for item in dimensions)
    if any(not isinstance(key, str) or not key for key in raw_keys):
        return None
    keys = tuple(sorted(raw_keys))
    if len(set(keys)) != len(keys):
        return None

    baseline = getattr(profile, "baseline_parameters", {})
    if not isinstance(baseline, Mapping) or set(baseline) != set(keys):
        return None

    return ExactParameterMaterializationPolicyV1(
        target_path=normalized_target,
        parameter_keys=keys,
    )


class DeterministicExactParameterExperimentExecutionPortV1:
    """Direct exact-parameter execution with strict delegation for everything else."""

    BACKEND = "DETERMINISTIC_EXACT_PARAMETER"

    def __init__(
        self,
        *,
        delegate: ExperimentExecutionPort,
        workspace: Path,
        state_root: Path,
        baseline_spec: ExperimentSpecV1,
        policy: ExactParameterMaterializationPolicyV1,
        clock: Callable[[], str],
        git: GitClient | None = None,
    ) -> None:
        if not isinstance(baseline_spec, ExperimentSpecV1):
            raise TypeError("baseline_spec must be ExperimentSpecV1")
        if not isinstance(policy, ExactParameterMaterializationPolicyV1):
            raise TypeError("policy must be ExactParameterMaterializationPolicyV1")
        self.delegate = delegate
        self.workspace = Path(workspace).resolve(strict=True)
        self.state_root = Path(state_root).resolve(strict=False)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.baseline_spec = baseline_spec
        self.policy = policy
        self.clock = clock
        self.git = git or GitClient()
        self.deterministic_calls: list[str] = []
        self.delegated_calls: list[str] = []

    @staticmethod
    def _same_mapping(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return canonical_json(plain(left)) == canonical_json(plain(right))

    def _supports_exact(self, spec: ExperimentSpecV1) -> bool:
        if set(spec.parameters) != set(self.policy.parameter_keys):
            return False
        if spec.candidate_patch_ref is not None:
            return False

        base = self.baseline_spec
        if (
            spec.strategy_id != base.strategy_id
            or spec.dataset_fingerprint != base.dataset_fingerprint
            or spec.evaluation_contract_hash != base.evaluation_contract_hash
            or spec.model_id != base.model_id
            or spec.provider != base.provider
            or spec.model_version != base.model_version
            or spec.prompt_hash != base.prompt_hash
            or spec.tool_policy_hash != base.tool_policy_hash
            or spec.seed != base.seed
        ):
            return False
        if not self._same_mapping(spec.sampling_config, base.sampling_config):
            return False
        if not self._same_mapping(spec.resource_budget, base.resource_budget):
            return False
        if not self._same_mapping(spec.execution, base.execution):
            return False

        execution_constraints = spec.execution.get("global_constraints", {})
        if not isinstance(execution_constraints, Mapping):
            return False
        if execution_constraints.get("selected_parameters_are_authoritative") is not True:
            return False
        allowed = execution_constraints.get("allowed_edit_files")
        if not isinstance(allowed, (tuple, list)):
            return False
        if tuple(allowed) != (self.policy.target_path,):
            return False
        return True

    @staticmethod
    def _git_timestamp(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExactParameterMaterializationError(
                "ExperimentSpec.created_at is not ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return f"@{int(parsed.timestamp())} +0000"

    @staticmethod
    def _git_env(index_path: Path | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if index_path is not None:
            env["GIT_INDEX_FILE"] = str(index_path)
        return env

    def _run_git(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.git.executable, "-C", str(self.workspace), *args],
                input=stdin,
                text=True,
                encoding="utf-8",
                errors="strict",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=self.git.timeout_seconds,
                env=None if env is None else dict(env),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise ExactParameterMaterializationError(
                f"git invocation failed: {type(exc).__name__}: {exc}"
            ) from exc
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ExactParameterMaterializationError(
                f"git {' '.join(args)} failed ({completed.returncode}): {detail}"
            )
        return completed

    def _text(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
    ) -> str:
        return self._run_git(*args, env=env).stdout.strip()

    def _resolve_base_commit(self, spec: ExperimentSpecV1) -> str:
        if len(spec.code_base_commit) != 40:
            raise ExactParameterMaterializationError(
                "exact parameter execution requires a full base commit"
            )
        resolved = self._text(
            "rev-parse",
            "--verify",
            f"{spec.code_base_commit}^{{commit}}",
        ).lower()
        if resolved != spec.code_base_commit:
            raise ExactParameterMaterializationError(
                "ExperimentSpec.code_base_commit does not resolve exactly"
            )
        if self._text("status", "--porcelain=v1", "--untracked-files=normal"):
            raise ExactParameterMaterializationError(
                "source workspace must be clean before deterministic materialization"
            )
        return resolved

    def _config_mode(self, base_commit: str) -> str:
        raw = self._text("ls-tree", base_commit, "--", self.policy.target_path)
        if not raw:
            raise ExactParameterMaterializationError(
                f"exact parameter target is absent from base commit: {self.policy.target_path}"
            )
        first, separator, listed_path = raw.partition("\t")
        fields = first.split()
        if not separator or len(fields) != 3 or fields[1] != "blob":
            raise ExactParameterMaterializationError(
                "exact parameter target must be a regular Git blob"
            )
        if listed_path.replace("\\", "/") != self.policy.target_path:
            raise ExactParameterMaterializationError(
                "exact parameter target path identity mismatch"
            )
        mode = fields[0]
        if mode not in {"100644", "100755"}:
            raise ExactParameterMaterializationError(
                f"unsupported exact parameter file mode: {mode}"
            )
        return mode

    @staticmethod
    def _canonical_parameter_json(spec: ExperimentSpecV1) -> str:
        return (
            json.dumps(
                plain(spec.parameters),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )

    def _candidate_ref(self, spec: ExperimentSpecV1) -> str:
        campaign_hash = stable_sha256(spec.campaign_id)[:16]
        experiment_hash = stable_sha256(spec.experiment_id)[:24]
        return (
            "refs/swarm-research/exact-parameter/"
            f"{campaign_hash}/{experiment_hash}"
        )

    def _materialization_dir(self, spec: ExperimentSpecV1) -> Path:
        return self.state_root / spec.campaign_id / spec.experiment_id

    def _persist_manifest(
        self,
        spec: ExperimentSpecV1,
        payload: Mapping[str, Any],
    ) -> None:
        root = self._materialization_dir(spec)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "materialization.json"
        rendered = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        if path.exists():
            if path.read_text(encoding="utf-8") != rendered:
                raise ExactParameterMaterializationError(
                    "existing deterministic materialization manifest mismatch"
                )
            return

        temp = path.with_name(path.name + f".tmp-{os.getpid()}")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def _materialize(
        self,
        spec: ExperimentSpecV1,
    ) -> tuple[str, str, Mapping[str, Any]]:
        base_commit = self._resolve_base_commit(spec)
        source_head_before = self._text("rev-parse", "HEAD").lower()
        mode = self._config_mode(base_commit)
        content = self._canonical_parameter_json(spec)

        root = self._materialization_dir(spec)
        root.mkdir(parents=True, exist_ok=True)
        index_path = root / "materialization.index"
        index_path.unlink(missing_ok=True)

        try:
            index_env = self._git_env(index_path)
            self._run_git("read-tree", base_commit, env=index_env)
            blob = self._run_git(
                "hash-object",
                "-w",
                "--stdin",
                stdin=content,
            ).stdout.strip()
            self._run_git(
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                blob,
                self.policy.target_path,
                env=index_env,
            )
            tree = self._text("write-tree", env=index_env)
        finally:
            index_path.unlink(missing_ok=True)

        base_tree = self._text("rev-parse", f"{base_commit}^{{tree}}")
        if tree == base_tree:
            raise ExactParameterMaterializationError(
                "exact parameter materialization produced a no-op tree"
            )

        git_date = self._git_timestamp(spec.created_at)
        commit_env = self._git_env()
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": "SwarmAgents",
                "GIT_AUTHOR_EMAIL": "swarmagents@local.invalid",
                "GIT_COMMITTER_NAME": "SwarmAgents",
                "GIT_COMMITTER_EMAIL": "swarmagents@local.invalid",
                "GIT_AUTHOR_DATE": git_date,
                "GIT_COMMITTER_DATE": git_date,
            }
        )
        message = (
            "SwarmAgents exact parameter experiment "
            f"{spec.fingerprint}\n"
        )
        candidate = self._run_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            env=commit_env,
            stdin=message,
        ).stdout.strip().lower()
        if len(candidate) != 40:
            raise ExactParameterMaterializationError(
                "git commit-tree did not return a full candidate commit"
            )

        changed = tuple(
            line.strip().replace("\\", "/")
            for line in self._text(
                "diff",
                "--name-only",
                f"{base_commit}..{candidate}",
            ).splitlines()
            if line.strip()
        )
        if changed != (self.policy.target_path,):
            raise ExactParameterMaterializationError(
                "deterministic materialization changed unexpected paths: "
                f"{changed}"
            )

        try:
            committed = json.loads(
                self._text(
                    "show",
                    f"{candidate}:{self.policy.target_path}",
                )
            )
        except json.JSONDecodeError as exc:
            raise ExactParameterMaterializationError(
                "candidate exact parameter JSON cannot be parsed"
            ) from exc
        if canonical_json(committed) != canonical_json(plain(spec.parameters)):
            raise ExactParameterMaterializationError(
                "candidate JSON does not equal authoritative experiment parameters"
            )

        candidate_ref = self._candidate_ref(spec)
        existing_ref = self._run_git(
            "rev-parse",
            "--verify",
            candidate_ref,
            check=False,
        )
        if existing_ref.returncode == 0:
            if existing_ref.stdout.strip().lower() != candidate:
                raise ExactParameterMaterializationError(
                    "existing deterministic candidate ref points to a different commit"
                )
        else:
            self._run_git("update-ref", candidate_ref, candidate)

        ancestry = self._run_git(
            "merge-base",
            "--is-ancestor",
            base_commit,
            candidate,
            check=False,
        )
        if ancestry.returncode != 0:
            raise ExactParameterMaterializationError(
                "deterministic candidate is not descended from the exact base commit"
            )

        source_head_after = self._text("rev-parse", "HEAD").lower()
        if source_head_after != source_head_before:
            raise ExactParameterMaterializationError(
                "deterministic materialization changed source workspace HEAD"
            )
        if self._text("status", "--porcelain=v1", "--untracked-files=normal"):
            raise ExactParameterMaterializationError(
                "deterministic materialization dirtied source workspace"
            )

        parameter_fingerprint = stable_sha256(plain(spec.parameters))
        manifest = {
            "schema_version": 1,
            "materialization_kind": "EXACT_PARAMETER_JSON",
            "campaign_id": spec.campaign_id,
            "experiment_id": spec.experiment_id,
            "experiment_fingerprint": spec.fingerprint,
            "base_commit": base_commit,
            "candidate_commit": candidate,
            "candidate_ref": candidate_ref,
            "target_path": self.policy.target_path,
            "parameter_keys": list(self.policy.parameter_keys),
            "parameter_fingerprint": parameter_fingerprint,
            "blob_sha": blob,
            "tree_sha": tree,
        }
        self._persist_manifest(spec, manifest)
        return candidate, candidate_ref, manifest

    def execute(
        self,
        spec: ExperimentSpecV1,
        *,
        resume: bool = False,
    ) -> ExperimentExecutionOutcomeV1:
        if not isinstance(spec, ExperimentSpecV1):
            raise TypeError("spec must be ExperimentSpecV1")
        if not isinstance(resume, bool):
            raise TypeError("resume must be bool")

        if not self._supports_exact(spec):
            self.delegated_calls.append(spec.experiment_id)
            return self.delegate.execute(spec, resume=resume)

        started = self.clock()
        try:
            candidate, candidate_ref, manifest = self._materialize(spec)
            ended = self.clock()
            self.deterministic_calls.append(spec.experiment_id)
            return ExperimentExecutionOutcomeV1(
                status=ExperimentExecutionStatusV1.COMPLETED,
                started_at=started,
                ended_at=ended,
                verification_outcome=None,
                candidate_commit=candidate,
                telemetry={
                    "execution_backend": self.BACKEND,
                    "materialization_kind": manifest["materialization_kind"],
                    "verification_authority": "COMMAND_METRIC_EVALUATOR",
                    "research_execution_mode": spec.execution.get("mode"),
                    "research_resume_requested": resume,
                    "candidate_ref": candidate_ref,
                    "target_path": self.policy.target_path,
                    "parameter_fingerprint": manifest["parameter_fingerprint"],
                    "worker_calls": 0,
                    "model_calls": 0,
                    "replans": 0,
                    "escalations": 0,
                },
            )
        except Exception as exc:
            ended = self.clock()
            return ExperimentExecutionOutcomeV1(
                status=ExperimentExecutionStatusV1.CRASHED,
                started_at=started,
                ended_at=ended,
                telemetry={
                    "execution_backend": self.BACKEND,
                    "materialization_kind": "EXACT_PARAMETER_JSON",
                    "verification_authority": "COMMAND_METRIC_EVALUATOR",
                    "research_execution_mode": spec.execution.get("mode"),
                    "research_resume_requested": resume,
                    "target_path": self.policy.target_path,
                    "worker_calls": 0,
                    "model_calls": 0,
                    "replans": 0,
                    "escalations": 0,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                failure_kind=ExperimentFailureKindV1.IMPLEMENTATION_BUG,
            )


__all__ = [
    "DeterministicExactParameterExperimentExecutionPortV1",
    "ExactParameterMaterializationError",
    "ExactParameterMaterializationPolicyV1",
    "exact_parameter_policy_from_profile",
]
