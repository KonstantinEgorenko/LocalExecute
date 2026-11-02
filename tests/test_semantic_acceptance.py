from dataclasses import FrozenInstanceError, replace
import json
import pytest

from harness.verification.acceptance import (build_acceptance_obligations, assess_acceptance_rows,
    AcceptanceObligationsV1, SemanticProvenanceV1, independent_obligation_refs)
from harness.judge import JudgeEvidenceResolver, JudgePolicy, JudgeProtocolError, parse_judge_response
from harness.judge.prompt import JudgePromptBuilder
from harness.judge.protocol import judge_response_schema
from harness.verification import VerificationOutcome, GateOutcome
from tests.judge_helpers import setup_failed_with_critic, valid_failed_judge_response


def bundle(tmp_path, objective='Update f to return the input plus one, preserving its public API.'):
    _,_,manager,store,artifacts,contract,lease,_,_=setup_failed_with_critic(tmp_path)
    original=JudgeEvidenceResolver(run_store=store,artifact_store=artifacts,manager=manager,policy=JudgePolicy()).resolve(
        run_id='r',contract=contract,lease_id=lease.lease_id)
    contract=replace(contract,objective=objective)
    frozen=build_acceptance_obligations(run_id='r',contract=contract,
        sources={'target.py':'def f(x):\n    """Return input plus one; preserve integer inputs."""\n    return x - 1\n'},
        tests={'test_target.py':'def test_old():\n    assert f(0) == 1\n'})
    report=replace(original.verification_report,outcome=VerificationOutcome.PASSED,
        results=tuple(replace(g,outcome=GateOutcome.PASSED) for g in original.verification_report.results))
    return replace(original,objective=objective,verification_report=report,acceptance_obligations=frozen)


def response(b,status='SATISFIED',basis=None,decision='ACCEPT'):
    value=json.loads(valid_failed_judge_response())
    value.update(deterministic_outcome=b.deterministic_outcome.value,decision=decision,
        next_action='NONE' if decision=='ACCEPT' else 'REPLAN',handoff_guidance=[])
    value['acceptance_evidence']=[{'obligation_id':o.obligation_id,'status':status,
        'candidate_evidence':'candidate_diff',
        'evidence_refs':['verification_report'] if o.kind=='VERIFICATION' else ['candidate_diff','generated_test_diff'],
        'expectation_basis':sorted(independent_obligation_refs(b.acceptance_obligations,o)) if basis is None else basis,
        'summary':'Public requirement independently supports this expectation; candidate diff checked.'}
        for o in b.acceptance_obligations.obligations]
    return value


def test_frozen_public_contract_is_distinct_from_buggy_expression_and_hypothesis(tmp_path):
    b=bundle(tmp_path); frozen=b.acceptance_obligations
    assert any(e.provenance is SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE and 'plus one' in e.content for e in frozen.evidence)
    assert not any(o.kind=='BEHAVIOR' and 'x - 1' in o.requirement for o in frozen.obligations)
    assert AcceptanceObligationsV1.from_dict(json.loads(json.dumps(frozen.to_dict())))==frozen
    with pytest.raises(FrozenInstanceError):frozen.run_id='other'


def test_objective_can_use_its_frozen_public_contract_without_erasing_behavioral_requirements(tmp_path):
    b=bundle(tmp_path); raw=response(b)
    doc=next(e.evidence_id for e in b.acceptance_obligations.evidence if e.provenance is SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE)
    root=next(r for r in raw['acceptance_evidence'] if r['obligation_id']=='objective')
    root['expectation_basis']=[doc]
    assert parse_judge_response(json.dumps(raw),b).decision.value=='ACCEPT'
    root['expectation_basis']=['generated_test_diff']
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(raw),b)


@pytest.mark.parametrize('pointer',[True,None,'another_candidate'])
def test_candidate_binding_is_exact_and_fail_closed(tmp_path,pointer):
    b=bundle(tmp_path); raw=response(b)
    raw['acceptance_evidence'][0]['candidate_evidence']=pointer
    with pytest.raises(JudgeProtocolError,match='candidate_diff'):
        parse_judge_response(json.dumps(raw),b)


@pytest.mark.parametrize('basis',[['generated_test_diff'],['candidate_diff'],['explorer_hypotheses'],['original_source_0']])
def test_self_confirming_expectations_cannot_accept(tmp_path,basis):
    b=bundle(tmp_path); raw=response(b,basis=basis)
    rows=assess_acceptance_rows(b.acceptance_obligations,raw['acceptance_evidence'])
    assert all(r.status=='UNRESOLVED' and r.support=='NON_INDEPENDENT' for r in rows)
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(raw),b)


def test_valid_independent_generated_regression_can_accept(tmp_path):
    b=bundle(tmp_path)
    result=parse_judge_response(json.dumps(response(b)),b)
    assert result.decision.value=='ACCEPT'
    assert all(e.status=='SATISFIED' and e.support=='INDEPENDENT' for e in result.acceptance_evidence)
    assert result.from_dict(result.to_dict())==result


def test_contract_contradiction_cannot_be_cancelled_by_green_generated_test(tmp_path):
    b=bundle(tmp_path); raw=response(b)
    item=next(r for r in raw['acceptance_evidence'] if r['obligation_id'].startswith('public_contract'))
    item.update(status='VIOLATED',summary='Candidate subtracts one and generated test endorses it; independent contract says add one.')
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(raw),b)
    raw.update(decision='REPLAN',next_action='REPLAN')
    assert any(e.status=='VIOLATED' for e in parse_judge_response(json.dumps(raw),b).acceptance_evidence)


def test_ambiguous_semantics_remain_unresolved_and_replan(tmp_path):
    b=bundle(tmp_path,objective='Update processing to the desired result, which is not specified.')
    raw=response(b,status='UNRESOLVED',decision='REPLAN')
    assert parse_judge_response(json.dumps(raw),b).decision.value=='REPLAN'
    raw.update(decision='ACCEPT',next_action='NONE')
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(raw),b)


def test_failed_syntax_verification_cannot_be_overridden_by_semantic_acceptance(tmp_path):
    b=bundle(tmp_path)
    failed=replace(b.verification_report,outcome=VerificationOutcome.FAILED,
        results=tuple(replace(g,outcome=GateOutcome.FAILED) for g in b.verification_report.results))
    b=replace(b,verification_report=failed)
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(response(b)),b)


@pytest.mark.parametrize('mutation',['missing','duplicate','invented_ref','false_schema_claim'])
def test_mapping_is_exact_bounded_and_provenance_is_harness_owned(tmp_path,mutation):
    b=bundle(tmp_path); raw=response(b)
    if mutation=='missing':raw['acceptance_evidence'].pop()
    elif mutation=='duplicate':raw['acceptance_evidence'][-1]=raw['acceptance_evidence'][0]
    elif mutation=='invented_ref':raw['acceptance_evidence'][0]['expectation_basis']=['hidden_oracle']
    else:raw['acceptance_evidence'][0]['support']='INDEPENDENT'
    with pytest.raises(JudgeProtocolError):parse_judge_response(json.dumps(raw),b)


def test_judge_input_contains_original_contract_tests_and_separate_postmutation_diff(tmp_path):
    b=bundle(tmp_path)
    _,text=JudgePromptBuilder(JudgePolicy()).prompts(b)
    payload=json.loads(text.split('\n',1)[1])
    assert payload['frozen_acceptance_obligations']==json.loads(json.dumps(b.acceptance_obligations.to_dict()))
    assert 'x - 1' in text and 'PREEXISTING_TEST_EVIDENCE' in text
    assert payload['semantic_rules']['verification_pass_is_not_semantic_acceptance']
    assert payload['response_contract']==judge_response_schema(b)


def test_missing_premutation_snapshot_cannot_be_reconstructed_from_candidate(tmp_path):
    b=bundle(tmp_path)
    frozen=replace(b.acceptance_obligations,unresolved_semantics=('Missing pre-mutation snapshot',))
    b=replace(b,acceptance_obligations=frozen)
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(response(b)),b)


def test_native_freeze_precedes_first_mutation_and_survives_restore(tmp_path,monkeypatch):
    from test_production_direct_integration import configured
    from harness.integration.acceptance_bridge import load_frozen_acceptance, freeze_acceptance
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    original=executor.execute
    captured=[]
    def execute(request):
        frozen=load_frozen_acceptance(store,'r',todo.work_contract)
        assert frozen is not None
        assert next(e.content for e in frozen.evidence if e.evidence_id=='original_source_0')=='value = 1\n'
        captured.append(request.messages[1].content)
        return original(request)
    executor.execute=execute
    assert adapter.execute(todo,profile_id='p',attempt_index=1).finalized
    assert all('FROZEN ACCEPTANCE OBLIGATIONS' in text for text in captured)
    session=adapter.owner.legacy.workspace.resolve(run_id='r',contract=todo.work_contract)
    initial=load_frozen_acceptance(store,'r',todo.work_contract)
    assert freeze_acceptance(store=store,run_id='r',contract=todo.work_contract,session=session,
        sources=('source.py',),tests=('tests/test_source.py',))==initial
    assert next(e.content for e in initial.evidence if e.evidence_id=='original_source_0')=='value = 1\n'


def test_shared_initial_boundary_preserves_public_contract_before_candidate(tmp_path,monkeypatch):
    from test_production_direct_integration import configured
    from harness.integration.acceptance_bridge import freeze_initial_acceptance
    adapter,todo,store,_,_=configured(tmp_path,monkeypatch)
    (tmp_path/'source.py').write_text('def f(x):\n    """Return input plus one."""\n    return x-1\n')
    session=adapter.owner.legacy.workspace.resolve(run_id='r',contract=todo.work_contract)
    initial=freeze_initial_acceptance(store=store,run_id='r',contract=todo.work_contract,session=session)
    (tmp_path/'source.py').write_text('def f(x):\n    """Return input minus one."""\n    return x-1\n')
    assert freeze_initial_acceptance(store=store,run_id='r',contract=todo.work_contract,session=session)==initial
    assert any(e.content=='Return input plus one.' for e in initial.evidence)
    assert not any(e.content=='Return input minus one.' for e in initial.evidence)


def test_oversized_public_context_is_unresolved_not_silently_partial(tmp_path,monkeypatch):
    from test_production_direct_integration import configured
    from harness.integration.acceptance_bridge import freeze_initial_acceptance
    adapter,todo,store,_,_=configured(tmp_path,monkeypatch)
    (tmp_path/'source.py').write_text('# large public file\n'+'# padding\n'*4000)
    session=adapter.owner.legacy.workspace.resolve(run_id='r',contract=todo.work_contract)
    frozen=freeze_initial_acceptance(store=store,run_id='r',contract=todo.work_contract,session=session)
    assert frozen.unresolved_semantics
    assert all(e.provenance is SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT for e in frozen.evidence)


def test_public_contract_obligation_cannot_be_closed_by_objective_citation_only(tmp_path):
    b=bundle(tmp_path); raw=response(b)
    row=next(r for r in raw['acceptance_evidence'] if r['obligation_id'].startswith('public_contract'))
    row['expectation_basis']=['original_objective']
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(raw),b)


def test_generated_test_diff_is_actual_bounded_candidate_section():
    from harness.judge.evidence import _generated_test_diff
    diff='diff --git a/source.py b/source.py\n+source\ndiff --git a/tests/test_source.py b/tests/test_source.py\n+test\n'
    assert _generated_test_diff(diff)=='diff --git a/tests/test_source.py b/tests/test_source.py\n+test\n'


def test_candidate_free_interpretation_cannot_see_implementation_or_generated_hypotheses(tmp_path):
    from harness.verification.acceptance import public_interpretation_request
    from harness.judge.protocol import parse_public_interpretations
    b=bundle(tmp_path); frozen=b.acceptance_obligations
    _,text,schema=public_interpretation_request(frozen)
    assert 'x - 1' not in text and 'OBSERVED_IMPLEMENTATION' not in text
    assert 'Return input plus one' in text and 'candidate_present":false' in text
    payload={'interpretations':{o.obligation_id:{'obligation_id':o.obligation_id,'expected_behavior':'Public input plus one.',
        'public_refs':list(o.evidence_refs),'unresolved':False,'witness':None} for o in frozen.obligations if o.kind!='VERIFICATION'}}
    rows=parse_public_interpretations(json.dumps(payload),frozen)
    assert all(r.provenance=='MODEL_HYPOTHESIS' and r.candidate_independent for r in rows)
    enriched=replace(frozen,public_interpretations=rows)
    assert enriched.obligations==frozen.obligations and enriched.evidence==frozen.evidence
    assert AcceptanceObligationsV1.from_dict(json.loads(json.dumps(enriched.to_dict())))==enriched
    payload['interpretations']['objective']['public_refs']=['original_source_0']
    with pytest.raises(ValueError,match='non-public'):
        parse_public_interpretations(json.dumps(payload),frozen)


def test_public_interpretation_is_one_call_and_durably_reused(tmp_path):
    from harness.judge import JudgeCoordinator
    from tests.judge_helpers import FakeJudgeRuntime
    b=bundle(tmp_path)
    _,_,manager,store,artifacts,_,_,_,_=setup_failed_with_critic(tmp_path/'coordinator')
    rows={o.obligation_id:{'obligation_id':o.obligation_id,'expected_behavior':'Public input plus one.',
        'public_refs':list(o.evidence_refs),'unresolved':False,'witness':None} for o in b.acceptance_obligations.obligations if o.kind!='VERIFICATION'}
    runtime=FakeJudgeRuntime([json.dumps({'interpretations':rows})])
    coordinator=JudgeCoordinator(run_store=store,artifact_store=artifacts,manager=manager,runtime=runtime,policy=JudgePolicy())
    first=coordinator.interpret_public_requirements(b.acceptance_obligations)
    assert first.public_interpretations and runtime.calls==1
    from harness.protocol_transport import ollama_compatible_schema
    from harness.verification.acceptance import public_interpretation_request
    assert runtime.requests[0][2]==ollama_compatible_schema(public_interpretation_request(b.acceptance_obligations)[2])
    assert 'maxLength' not in json.dumps(runtime.requests[0][2])
    assert coordinator.interpret_public_requirements(b.acceptance_obligations)==first and runtime.calls==1


def test_unsupported_public_interpretation_stays_unresolved(tmp_path):
    from harness.verification.acceptance import PublicInterpretationV1
    b=bundle(tmp_path)
    frozen=replace(b.acceptance_obligations,public_interpretations=(
        PublicInterpretationV1('objective','No supported intended behavior.',('original_objective',),True),))
    b=replace(b,acceptance_obligations=frozen)
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(response(b)),b)


@pytest.mark.parametrize('status',['VIOLATED','UNRESOLVED'])
def test_final_judge_cannot_cancel_isolated_contract_disagreement(tmp_path,status):
    b=bundle(tmp_path)
    isolated=tuple(r for r in assess_acceptance_rows(b.acceptance_obligations,response(b,status=status)['acceptance_evidence'])
        if r.obligation_id!='verification')
    guarded=replace(b,semantic_assessment=isolated)
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(response(guarded)),guarded)
    assert parse_judge_response(json.dumps(response(guarded,decision='REPLAN')),guarded).decision.value=='REPLAN'


def test_public_witness_is_durably_reused_without_an_extra_judge_call(tmp_path):
    from harness.judge import JudgeCoordinator
    from harness.verification.public_witness import PublicWitnessObservationV1
    from tests.judge_helpers import FakeJudgeRuntime
    b=bundle(tmp_path/'bundle')
    _,_,manager,store,artifacts,_,_,_,_=setup_failed_with_critic(tmp_path/'coordinator')
    class Port:
        calls=0
        def evaluate(self, **kwargs):
            self.calls+=1
            return tuple(PublicWitnessObservationV1(o.obligation_id,'MATCH','2')
                for o in kwargs['frozen'].obligations if o.kind!='VERIFICATION')
    port=Port();runtime=FakeJudgeRuntime([])
    judge=JudgeCoordinator(run_store=store,artifact_store=artifacts,manager=manager,runtime=runtime,
        policy=JudgePolicy(),require_acceptance=True,public_witness_port=port)
    first,calls=judge.compare_public_contract(b,object())
    assert calls==0 and not first.semantic_assessment
    assert first.public_witness_observations and port.calls==1 and runtime.calls==0
    second,calls=judge.compare_public_contract(b,object())
    assert second==first and calls==0 and port.calls==1
    assert first.request_fingerprint!=b.request_fingerprint


def test_unavailable_public_witness_does_not_blanket_accept(tmp_path):
    from harness.judge import JudgeCoordinator
    from tests.judge_helpers import FakeJudgeRuntime
    b=bundle(tmp_path/'bundle')
    _,_,manager,store,artifacts,_,_,_,_=setup_failed_with_critic(tmp_path/'coordinator')
    judge=JudgeCoordinator(run_store=store,artifact_store=artifacts,manager=manager,runtime=FakeJudgeRuntime([]),
        policy=JudgePolicy(),require_acceptance=True)
    guarded,_=judge.compare_public_contract(b)
    assert all(r.status=='UNRESOLVED' for r in guarded.semantic_assessment)
    assert 'ACCEPT' not in judge_response_schema(guarded)['properties']['decision']['enum']
    with pytest.raises(JudgeProtocolError,match='cannot ACCEPT'):
        parse_judge_response(json.dumps(response(guarded)),guarded)


def test_resumed_worker_cannot_relabel_candidate_content_as_premutation_public_baseline(tmp_path,monkeypatch):
    from test_production_direct_integration import configured
    from harness.integration.acceptance_bridge import freeze_initial_acceptance
    from harness.control_plane import QuotaUsage
    adapter,todo,store,_,_=configured(tmp_path,monkeypatch)
    store.update_run(replace(store.get_run('r'),quota_usage=QuotaUsage(worker_calls=1)))
    (tmp_path/'source.py').write_text('def f(x):\n    """Candidate invented normative claim."""\n    return x-1\n')
    session=adapter.owner.legacy.workspace.resolve(run_id='r',contract=todo.work_contract)
    frozen=freeze_initial_acceptance(store=store,run_id='r',contract=todo.work_contract,session=session)
    assert frozen.unresolved_semantics
    assert all(e.provenance is SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT for e in frozen.evidence)
