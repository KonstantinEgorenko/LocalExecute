from __future__ import annotations

import json

from harness.context_manager.models import ContextBudget, ContextBundle
from harness.control_plane import Quota, WorkContract
from harness.executor.models import ExecutionOutcome, WorkerResult
from harness.router.scoring import DecisionType, RoutingDecision
from harness.simple_worker_v2 import (
    SimpleWorkerOutcomeV2,
    SimpleWorkerPolicyV2,
    SimpleWorkerV2,
)
from harness.workspace import WorkspacePolicy, WorkspaceSession


def decision(kind=DecisionType.WORKER):
    return RoutingDecision(
        kind,
        "qwen35" if kind is DecisionType.WORKER else None,
        100 if kind is DecisionType.WORKER else None,
        1.0,
        "test route",
        (),
        None,
        None,
        (),
    )


def worker(content: str, model="qwen35"):
    return WorkerResult(
        ExecutionOutcome.SUCCESS,
        model,
        model + ":tag",
        content,
        None,
        (),
        None,
        None,
        None,
        None,
        None,
        None,
        "stop",
        (),
        None,
        None,
        None,
        None,
        None,
    )


class Executor:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def action(name, arguments, summary=None):
    if name == "EDIT":
        arguments = {"match": {"kind": arguments.get("kind", "CREATE" if arguments["old_text"] is None else "FRAGMENT"),
                                "file": {"path": arguments["path"]},
                                **{key: arguments[key] for key in ("old_text", "expected_occurrences")}},
                     "new_text": arguments["new_text"]}
    return worker(
        json.dumps(
            {
                "schema_version": 1,
                "action": name,
                "arguments": arguments,
                "summary": summary,
            }
        )
    )


def setup(tmp_path, *, requires_mutation=False, allowed=("target.py", "new.py")):
    contract = WorkContract(
        "g",
        "t",
        "implement requested change",
        {"REQUIRES_WORKSPACE_MUTATION": requires_mutation},
        allowed,
        (".git", "state"),
        ("WORKER_RESULT", "CANDIDATE_DIFF"),
        Quota(12, 2, 2, 2, 3),
    )
    bundle = ContextBundle(
        "ctx",
        "req",
        "g",
        "t",
        (),
        ContextBudget(4096, 20, 100000),
    )
    session = WorkspaceSession(
        "s",
        "r",
        "t",
        str(tmp_path),
        WorkspacePolicy(
            allowed_paths=allowed,
            forbidden_paths=(".git", "state"),
            command_prefixes=(("python",),),
        ),
    )
    return contract, bundle, session


def test_finish_only_succeeds_when_mutation_not_required(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([action("FINISH", {}, "done")])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.final_summary == "done"
    assert result.model_turns == 1
    assert result.aci_calls == 0


def test_mutation_required_rejects_finish_then_accepts_edit(tmp_path):
    (tmp_path / "target.py").write_text("value = 1\n", encoding="utf-8")
    contract, bundle, session = setup(tmp_path, requires_mutation=True)
    executor = Executor([
        action("FINISH", {}, "too early"),
        action(
            "EDIT",
            {
                "path": "target.py",
                "old_text": "1",
                "new_text": "2",
                "expected_occurrences": 1,
            },
        ),
        action("FINISH", {}, "updated"),
    ])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.model_turns == 3
    assert result.aci_calls == 1
    assert result.changed_paths == ("target.py",)
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == "value = 2\n"
    second_prompt = "\n".join(m.content for m in executor.calls[1].messages)
    assert "COMPLETION_REJECTED" in second_prompt


def test_view_observation_hides_low_level_tool_names(tmp_path):
    (tmp_path / "target.py").write_text("value = 1\n", encoding="utf-8")
    contract, bundle, session = setup(tmp_path)
    executor = Executor([
        action("VIEW", {"path": "target.py"}),
        action("FINISH", {}, "inspected"),
    ])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    second_prompt = "\n".join(m.content for m in executor.calls[1].messages)
    assert "ACI_OBSERVATION" in second_prompt
    assert "value = 1" in second_prompt
    assert "READ_TEXT" not in second_prompt
    assert "low_level_tool" not in second_prompt


def test_syntax_preflight_error_is_observation_then_model_can_correct(tmp_path):
    (tmp_path / "target.py").write_text("value = 1\n", encoding="utf-8")
    contract, bundle, session = setup(tmp_path, requires_mutation=True)
    executor = Executor([
        action(
            "EDIT",
            {
                "path": "target.py",
                "old_text": "1",
                "new_text": "(1))",
                "expected_occurrences": 1,
            },
        ),
        action(
            "EDIT",
            {
                "path": "target.py",
                "old_text": "1",
                "new_text": "2",
                "expected_occurrences": 1,
            },
        ),
        action("FINISH", {}, "fixed"),
    ])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.aci_calls == 2
    assert result.changed_paths == ("target.py",)
    second_prompt = "\n".join(m.content for m in executor.calls[1].messages)
    assert "SYNTAX_PREFLIGHT_FAILED" in second_prompt
    assert "workspace_mutated" in second_prompt
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == "value = 2\n"


def test_protocol_error_gets_one_correction_turn(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([
        worker('{"action":"FINISH"}'),
        action("FINISH", {}, "recovered"),
    ])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.model_turns == 2
    second_prompt = "\n".join(m.content for m in executor.calls[1].messages)
    assert "PROTOCOL_ERROR" in second_prompt


def test_protocol_failure_limit_fails_closed(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([worker("{}"), worker("{}")])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.NEEDS_ATTENTION
    assert result.reason == "simple worker protocol failure limit reached"
    assert result.model_turns == 2


def test_model_turn_quota_is_hard_bound(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([
        action("VIEW", {"path": ""}),
        action("VIEW", {"path": ""}),
    ])
    result = SimpleWorkerV2(
        executor=executor,
        policy=SimpleWorkerPolicyV2(max_model_turns=2, max_aci_calls=10),
    ).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.QUOTA_EXHAUSTED
    assert result.model_turns == 2
    assert len(executor.calls) == 2


def test_aci_call_quota_is_hard_bound(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([
        action("VIEW", {"path": ""}),
        action("VIEW", {"path": ""}),
    ])
    result = SimpleWorkerV2(
        executor=executor,
        policy=SimpleWorkerPolicyV2(max_model_turns=5, max_aci_calls=1),
    ).execute(
        decision=decision(),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.QUOTA_EXHAUSTED
    assert result.aci_calls == 1
    assert result.model_turns == 2


def test_non_worker_decision_never_calls_executor(tmp_path):
    contract, bundle, session = setup(tmp_path)
    executor = Executor([])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=decision(DecisionType.ORCHESTRATOR),
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.NEEDS_ATTENTION
    assert result.model_turns == 0
    assert executor.calls == []


def test_worker_uses_same_fixed_routing_decision_and_structured_schema(tmp_path):
    contract, bundle, session = setup(tmp_path)
    chosen = decision()
    executor = Executor([
        action("VIEW", {"path": ""}),
        action("FINISH", {}, "done"),
    ])
    result = SimpleWorkerV2(executor=executor).execute(
        decision=chosen,
        contract=contract,
        context_bundle=bundle,
        workspace_session=session,
    )
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert all(call.routing_decision is chosen for call in executor.calls)
    assert all(call.temperature == 0.0 for call in executor.calls)
    assert all(call.response_format is not None for call in executor.calls)
    assert all(
        str(tmp_path.resolve()) not in "\n".join(m.content for m in call.messages)
        for call in executor.calls
    )
