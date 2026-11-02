import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from harness.control_plane import InMemoryRunStore, Run, RunStatus, QuotaUsage, Evidence, EvidenceStatus
from harness.context_manager import InMemoryArtifactStore
from harness.integration.ornith_planner_adapter import OrnithPlanningAdapter
from harness.integration.simple_worker_adapter import SimpleWorkerWorkflowAdapter
from harness.lead.models import LeadRequest
from harness.mission.models import MissionSpecV1
from harness.context_manager.models import ContextBundle, ContextBudget
from test_typed_scenario_projector_v1 import project, policies, OBJECTIVE
from test_typed_scenario_selector_v1 import Runtime, payload
from test_simple_worker_v2 import action, decision
from harness.workspace import WorkspaceSession, WorkspacePolicy


class Registry:
    def __init__(self):
        self.observation = None
        self.result = None

    def load_plan(self, spec):
        return self.observation

    def save_plan(self, spec, result, observation):
        self.result, self.observation = result, observation


def test_typed_plan_uses_selector_compiler_projector_and_durable_idempotency():
    runtime = Runtime(payload())
    store = InMemoryRunStore()
    registry = Registry()
    lead = NS(runtime=runtime, store=store, policies=policies())
    spec = MissionSpecV1("mission", "goal", OBJECTIVE, OBJECTIVE,
        global_constraints={"TYPED_SCENARIO_EXECUTION": True, "research_required": False})
    planner = OrnithPlanningAdapter(lead,
        lambda spec: LeadRequest(spec.raw_objective, spec.global_constraints),
        plan_registry=registry, required_gates=("candidate_diff", "python_syntax", "pytest"))
    first = planner.ensure_initial_plan(spec)
    assert planner.ensure_initial_plan(spec) == first
    assert len(runtime.calls) == 1 and len(first.todo_ids) == 1
    assert runtime.calls[0][2]["properties"]["requires_research"]["enum"] == [False]
    assert '"research_required":false' in runtime.calls[0][1]
    item = registry.result.work_items[0]
    assert item.work_contract.constraints["TYPED_SCENARIO_EXECUTION"] is True
    assert item.todo.required_gates == ("candidate_diff", "python_syntax", "pytest", "regression_change")
    assert store.get_todo(first.todo_ids[0]) == item.todo
    assert registry.result.model_telemetry["typed_scenario"]["telemetry"]["model_id"] == "ornith_lead"


def test_unsupported_typed_scenario_never_commits_plan():
    registry = Registry()
    lead = NS(runtime=Runtime(payload(scenario="RESEARCH_THEN_IMPLEMENT", requires_research=True)),
              store=InMemoryRunStore(), policies=policies())
    spec = MissionSpecV1("m", "g", OBJECTIVE, OBJECTIVE,
                        global_constraints={"TYPED_SCENARIO_EXECUTION": True})
    planner = OrnithPlanningAdapter(lead, lambda spec: LeadRequest(spec.raw_objective),
        plan_registry=registry, required_gates=("candidate_diff",))
    with pytest.raises(RuntimeError):
        planner.ensure_initial_plan(spec)
    assert registry.result is None


def setup_adapter(tmp_path, monkeypatch):
    import harness.integration.simple_worker_adapter as module
    (tmp_path / "tests").mkdir(exist_ok=True)
    item = project(req=LeadRequest(OBJECTIVE)).work_items[0]
    store = InMemoryRunStore()
    store.create_goal(project(req=LeadRequest(OBJECTIVE)).goal)
    store.create_todo(item.todo)
    store.create_run(Run("r", item.todo.goal_id, item.todo.todo_id, RunStatus.RUNNING, QuotaUsage()))
    session = WorkspaceSession("s", "r", item.todo.todo_id, str(tmp_path), WorkspacePolicy())
    executor = NS(values=[action("EDIT", {"path":"tests/test_new.py", "old_text":None,
        "new_text":"value = 2\n", "expected_occurrences":1}), action("FINISH", {}, "implemented")], calls=0)
    def execute(request):
        executor.calls += 1
        return executor.values.pop(0)
    executor.execute = execute
    trace = NS(events=[])
    trace.record = lambda *args, **kw: trace.events.append((args, kw))
    observer = NS(model_call_started=lambda **kw: None, model_call_finished=lambda **kw: None,
                  model_call_failed=lambda **kw: None)
    loop = NS(executor=NS(inner=executor), router=NS(route=lambda profile: decision()), observer=observer)
    bundle = ContextBundle("bundle", "request", item.todo.goal_id, item.todo.todo_id, (), ContextBudget(4096,20,100000))
    legacy = NS(workspace=NS(run_store=store,current=lambda todo_id:("r",NS(lease_id="lease")),
                            manager=NS(snapshot=lambda lease_id:NS(dirty_paths=())),
                            resolve=lambda **kw: session),
                agent=NS(loop=loop,context_resolver=NS(assemble=lambda *a, **kw: bundle)),
                policy=NS(profile=lambda p:NS(model_context_tokens=4096,max_items=20,max_bytes=100000)),
                _reopen_for_slice=lambda r:None, _agent_routing_spec=lambda t:item.routing_task_spec)
    artifacts = InMemoryArtifactStore()
    common = NS(artifacts=artifacts,router=object(),snapshot_provider=lambda:object())
    monkeypatch.setattr(module.TaskProfileFactory,"build",lambda *a, **kw:object())
    monkeypatch.setattr(module,"SelectedWorkerAdmissionExecutor",lambda **kw:kw["executor"])
    monkeypatch.setattr(module,"latest_repair_activation",lambda *a, **kw:None)
    adapter = SimpleWorkerWorkflowAdapter(legacy=legacy,common=common,trace_registry=NS(resolve_for_run=lambda r:trace))
    return adapter, item, store, executor


def test_simple_worker_publishes_real_mutation_and_result_leaves_verifier_authoritative(tmp_path, monkeypatch):
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    todo = NS(todo_id=item.todo.todo_id,work_contract=item.work_contract)
    kwargs = dict(profile_id="p",objective_with_context=OBJECTIVE,attempt_index=0,fresh_workspace=True)
    result = adapter.execute(todo, **kwargs)
    assert result.finalized and result.changed_paths == ("tests/test_new.py",)
    assert (tmp_path/"tests/test_new.py").read_text() == "value = 2\n"
    assert store.get_run("r").status is RunStatus.RUNNING
    assert store.get_run("r").quota_usage.worker_calls == 2
    events = [e.to_dict()["payload"] for e in store.list_events("r")]
    assert any(e.get("evidence_type") == "WORKER_RESULT" for e in events)
    assert not any(e.get("evidence_type") == "VERIFICATION_REPORT" for e in events)
    assert adapter.execute(todo, **kwargs) == result
    assert executor.calls == 2


def test_interrupted_epoch_never_blindly_replays_mutation(tmp_path, monkeypatch):
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    store.record_evidence("r",Evidence("interrupted",item.todo.todo_id,"SIMPLE_WORKER_EVENT",
        "SimpleWorkerV2",EvidenceStatus.PASSED,{"request_id":"r:simple-worker:0:p", "kind":"ACI_ACTION"}))
    result = adapter.execute(NS(todo_id=item.todo.todo_id,work_contract=item.work_contract),
        profile_id="p",objective_with_context=OBJECTIVE,attempt_index=0,fresh_workspace=False)
    assert not result.finalized and executor.calls == 0
    assert result.raw["failure_kind"] == "TOOL_FAILURE"


@pytest.mark.parametrize("initial_status", [RunStatus.RUNNING, RunStatus.NEEDS_ATTENTION])
def test_exhausted_fallback_cannot_reopen_or_verify_unfinished_candidate(tmp_path, monkeypatch, initial_status):
    from harness.integration.workflow_production_adapters import WorkflowVerificationAdapter
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    run = store.get_run("r")
    store.update_run(replace(run, status=initial_status,
        quota_usage=QuotaUsage(item.work_contract.quota.max_worker_calls, 0, 0, {})))
    source = "value = 2\n"
    (tmp_path / "dirty.py").write_text(source)
    def forbidden(*args, **kwargs):
        pytest.fail("exhausted invocation must not reopen, route, resolve context or verify")
    adapter.legacy._reopen_for_slice = forbidden
    adapter.legacy.agent.context_resolver.assemble = forbidden
    adapter.common.snapshot_provider = forbidden
    todo = NS(todo_id=item.todo.todo_id, work_contract=item.work_contract)
    kwargs = dict(profile_id="fallback", objective_with_context=OBJECTIVE,
                  attempt_index=3, fresh_workspace=False)
    result = adapter.execute(todo, **kwargs)
    assert not result.finalized and result.outcome == "QUOTA_EXHAUSTED"
    assert executor.calls == result.model_calls == result.tool_calls == 0
    assert store.get_run("r").status is RunStatus.NEEDS_ATTENTION
    assert (tmp_path / "dirty.py").read_text() == source
    evidence = [e.to_dict()["payload"] for e in store.list_events("r")]
    assert evidence[-1]["evidence_type"] == "SIMPLE_WORKER_EXECUTION"
    assert evidence[-1]["status"] == EvidenceStatus.FAILED.value
    assert not any(e.get("evidence_type") == "WORKER_RESULT" for e in evidence)
    assert adapter.execute(todo, **kwargs) == result
    assert len(store.list_events("r")) == len(evidence)
    verifier = WorkflowVerificationAdapter(coordinator=NS(verify=forbidden),
                                          workspace=adapter.legacy.workspace)
    verification = verifier.verify(todo, result)
    assert not verification.passed
    assert verification.raw["kind"] == "EXECUTION_NOT_VERIFIABLE"


def test_transient_pack_is_not_reembedded_in_durable_contract_context(tmp_path, monkeypatch):
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    resolver = adapter.legacy.agent.context_resolver
    original_assemble = resolver.assemble
    seen = []
    def assemble(run_id, contract, budget, **kwargs):
        seen.append(contract)
        return original_assemble(run_id, contract, budget, **kwargs)
    resolver.assemble = assemble
    objective = OBJECTIVE + '\nEXTERNAL_CONTEXT_V1\ndef example():\n    return "quoted source"\n'
    result = adapter.execute(NS(todo_id=item.todo.todo_id, work_contract=item.work_contract),
        profile_id="p", objective_with_context=objective, attempt_index=0, fresh_workspace=True)
    assert result.finalized
    assert seen == [item.work_contract]
    assert seen[0].objective == OBJECTIVE


def test_current_verification_feedback_is_required_in_next_worker_context(tmp_path, monkeypatch):
    adapter, item, _, _ = setup_adapter(tmp_path, monkeypatch)
    ref = object()
    # Authority validation has real-coordinator coverage; this test exercises
    # the resolved report/ref wiring into required ContextBundle artifacts.
    report = NS(report_id="verified-report", run_id="r", todo_id=item.todo.todo_id,
        lease_id="lease", candidate_head="head", candidate_diff_sha256="diff",
        plan_fingerprint="plan", results=())
    monkeypatch.setattr(adapter, "_resolved_verification_feedback", lambda *args: (report, ref))
    resolver = adapter.legacy.agent.context_resolver
    original = resolver.assemble
    seen = []
    def assemble(*args, **kwargs):
        seen.append(kwargs["required_artifacts"])
        return original(*args, **kwargs)
    resolver.assemble = assemble
    result = adapter.execute(NS(todo_id=item.todo.todo_id, work_contract=item.work_contract),
        profile_id="p", objective_with_context=OBJECTIVE, attempt_index=1, fresh_workspace=False)
    assert result.finalized
    assert seen == [(ref,)]


def test_protocol_failure_evidence_is_failed_and_retained(tmp_path, monkeypatch):
    from test_simple_worker_v2 import worker
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    executor.values = [worker("not JSON"), worker("not JSON")]
    result = adapter.execute(NS(todo_id=item.todo.todo_id, work_contract=item.work_contract),
        profile_id="p", objective_with_context=OBJECTIVE, attempt_index=0, fresh_workspace=True)
    assert not result.finalized and result.changed_paths == ()
    assert result.raw["failure_kind"] == "PROTOCOL_INVALID"
    evidence = [e.to_dict()["payload"] for e in store.list_events("r")
                if e.to_dict()["payload"].get("evidence_type") == "AGENT_PROTOCOL_FAILURE"]
    assert len(evidence) == 2
    assert all(e["status"] == EvidenceStatus.FAILED.value for e in evidence)


def test_no_effect_loop_cause_survives_adapter_and_durable_replay(tmp_path, monkeypatch):
    from test_simple_worker_v2_range_edit import range_edit
    from harness.integration.failure_policy import classify_failure, recovery_workspace_disposition
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    source = 'value = 1\n'
    (tmp_path / 'target.py').write_text(source)
    executor.values = [action('VIEW', {'path': 'target.py'}),
        range_edit(1, 1, ['value = 1']), range_edit(1, 1, ['value = 1'])]
    todo = NS(todo_id=item.todo.todo_id, work_contract=item.work_contract)
    kwargs = dict(profile_id='p', objective_with_context=OBJECTIVE, attempt_index=0, fresh_workspace=True)
    result = adapter.execute(todo, **kwargs)
    assert not result.finalized and result.tool_calls == 2 and result.model_calls == 3
    assert result.raw['failure_kind'] == classify_failure(execution=result).failure_kind == 'TOOL_LOOP'
    assert recovery_workspace_disposition(result.raw['failure_kind']).value == 'REUSE_CANDIDATE'
    assert store.get_run('r').status is RunStatus.NEEDS_ATTENTION
    evidence = [e.to_dict()['payload'] for e in store.list_events('r')]
    loop = next(e for e in evidence if e.get('payload', {}).get('kind') == 'TOOL_LOOP')
    assert loop['status'] == 'FAILED'
    assert loop['payload']['error_code'] == 'NO_EFFECT'
    restarted = SimpleWorkerWorkflowAdapter(legacy=adapter.legacy, common=adapter.common,
        trace_registry=adapter.trace_registry)
    assert restarted.execute(todo, **kwargs) == result
    assert executor.calls == 3
    assert (tmp_path / 'target.py').read_text() == source


def test_large_action_evidence_is_bounded_without_parsing_truncated_json(tmp_path, monkeypatch):
    adapter, item, store, executor = setup_adapter(tmp_path, monkeypatch)
    source = 'value = "' + 'x' * 20000 + '"\n'
    executor.values = [action("EDIT", {"path":"tests/test_new.py", "old_text":None,
        "new_text":source, "expected_occurrences":1}), action("FINISH", {}, "created")]
    result = adapter.execute(NS(todo_id=item.todo.todo_id, work_contract=item.work_contract),
        profile_id="p", objective_with_context=OBJECTIVE, attempt_index=0, fresh_workspace=True)
    assert result.finalized and (tmp_path / "tests/test_new.py").read_text() == source
    payloads = [e.to_dict()["payload"].get("payload", {}) for e in store.list_events("r")]
    event = next(p for p in payloads if p.get("kind") == "ACI_ACTION")
    assert event["action"] == "EDIT" and event["truncated"] is True
    assert len(event["raw"]) == 16384
