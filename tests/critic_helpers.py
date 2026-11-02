from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.context_manager import Artifact, ArtifactKind, ArtifactOrigin, FileArtifactStore
from harness.context_manager.integration import artifact_uri
from harness.control_plane import Evidence, EvidenceStatus, FileRunStore, Goal, GoalStatus, Quota, QuotaUsage, Run, RunStatus, Todo, TodoStatus, WorkContract
from harness.executor import ExecutionOutcome, WorkerResult
from harness.router.scoring import DecisionType, RoutingDecision
from harness.verification import GateExecutionResult, GateOutcome, VerificationGateType, VerificationOutcome, VerificationReport, VerificationPublisher
from harness.worktree import GitWorktreeManager


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.email", "phase8@example.invalid")
    git(path, "config", "user.name", "Phase8")
    (path / "target.py").write_text('VALUE = "OLD"\n', encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    return path


def setup_failed(tmp_path: Path):
    control = init_repo(tmp_path / "control")
    external = init_repo(tmp_path / "external")
    manager = GitWorktreeManager(protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "worktree-state")
    run_store = FileRunStore(tmp_path / "cp")
    run_store.create_goal(Goal("g", "fix target", GoalStatus.ACTIVE, "now"))
    run_store.create_todo(Todo("t", "g", "replace OLD with EXPECTED", TodoStatus.RUNNING, (), (), 0))
    run_store.create_run(Run("r", "g", "t", RunStatus.RUNNING, QuotaUsage()))
    contract = WorkContract("g", "t", "replace OLD with EXPECTED", {}, ("target.py",), (), (), Quota(6, 2, 2, 2, 3))
    lease = manager.acquire(source_repo_root=external, run_id="r", todo_id="t")
    target = Path(lease.worktree_root, "target.py")
    target.write_text('VALUE = "WRONG"\n', encoding="utf-8")
    snapshot = manager.snapshot(lease.lease_id)
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    result = GateExecutionResult("exact_target", VerificationGateType.POSTCONDITION, True, GateOutcome.FAILED, 0, "expected EXPECTED; observed WRONG")
    report = VerificationReport(
        "verification-failed", "plan", "a" * 64, "r", "t", lease.lease_id, snapshot.head_commit, snapshot.diff_sha256,
        snapshot.dirty_paths, "start", "end", (result,), VerificationOutcome.FAILED,
        candidate_content_sha256=snapshot.content_sha256, candidate_file_sha256=snapshot.file_sha256,
    )
    VerificationPublisher(run_store=run_store, artifact_store=artifacts).publish(report)
    run_store.update_run(Run("r", "g", "t", RunStatus.FAILED, QuotaUsage()))
    worker_content = json.dumps({"summary": "changed OLD to WRONG", "changed_paths": ["target.py"], "model_id": "deepseek_fast_coder"}, sort_keys=True)
    worker_artifact = Artifact.from_text("r:worker-result:0", ArtifactKind.WORKER_RESULT, ArtifactOrigin.WORKER, worker_content, "application/json", "CodingAgentLoop", "now", ("target.py",))
    artifacts.create_artifact(worker_artifact)
    run_store.record_evidence("r", Evidence(
        "r:worker-result:0", "t", "WORKER_RESULT", "CodingAgentLoop", EvidenceStatus.PASSED,
        {"summary": "changed OLD to WRONG", "model_id": "deepseek_fast_coder", "changed_paths": ["target.py"]}, artifact_uri(worker_artifact.artifact_id),
    ))
    return control, external, manager, run_store, artifacts, contract, lease, report


class FakeRouter:
    def __init__(self, selected_model: str = "ministral_reasoner") -> None:
        self.selected_model = selected_model
        self.profiles = []

    def route(self, profile):
        self.profiles.append(profile)
        return RoutingDecision(DecisionType.WORKER, self.selected_model, 100, 1.0, "fake critic route", (), {"mode": "LOAD_DIRECT", "requested_vram_gb": 10.5}, None, ())


class FakeExecutor:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        return WorkerResult(
            outcome=ExecutionOutcome.SUCCESS,
            model_id="ministral_reasoner",
            model_tag="ministral-3:14b",
            content=content,
            thinking=None,
            tool_calls=(),
            prompt_eval_count=100,
            eval_count=40,
            load_duration=1,
            prompt_eval_duration=2,
            eval_duration=3,
            total_duration=6,
            done_reason="stop",
            lifecycle_actions=(),
            requested_context_length=16384,
            observed_context_length=16384,
            configured_num_gpu=None,
            before_runtime_state=None,
            after_runtime_state=None,
        )


def valid_failed_response(evidence_id: str = "verification-failed:evidence") -> str:
    return json.dumps({
        "deterministic_outcome": "FAILED",
        "verdict": "RETRY",
        "failure_class": "IMPLEMENTATION_ERROR",
        "confidence": 0.95,
        "summary": "The exact postcondition failed because the implementation wrote the wrong value.",
        "evidence": [evidence_id],
        "recommended_action": "RETRY_WITH_CONTEXT",
        "retry_guidance": ["Match the exact expected value while preserving allowed scope."],
        "can_override_deterministic": False,
    }, separators=(",", ":"))
