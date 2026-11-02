from dataclasses import replace
from types import SimpleNamespace as NS
import json
import pytest

from harness.simple_worker_v2.edit_intent import resolve_edit_intent, DiscoveryError, TypedEditIntentV1
from harness.simple_worker_v2.discovery import SimpleDiscoveryV1, discovery_schema
from harness.simple_worker_v2.interaction_state import InteractionStateV1, fingerprint, encoded
from harness.workspace_aci_v2 import WorkspaceAciV2, AciActionV2, AciActionKindV2
from harness.workspace import WorkspacePolicy
from test_simple_worker_v2 import action, decision, worker
from test_production_direct_integration import configured
from test_edit_transport_experiment import native


def setup(tmp_path, monkeypatch):
    direct,todo,store,executor,reports=configured(tmp_path,monkeypatch)
    owner=direct.owner
    owner.simple_native_mutation=True
    owner.common.executor=executor
    todo.work_contract=replace(todo.work_contract,constraints={**todo.work_contract.constraints,
        'EXECUTION_DEPTH_POLICY':False})
    session=owner.legacy.workspace.resolve(run_id='r',contract=todo.work_contract)
    state=InteractionStateV1('r',todo.todo_id,fingerprint(encoded(todo.work_contract.to_dict())))
    return owner,todo,store,executor,reports,session,state


def viewed(state,session,*paths):
    for path in paths:
        aci=AciActionV2(AciActionKindV2.VIEW,path=path)
        obs=WorkspaceAciV2().execute(session=session,call_id='view',action=aci)
        assert obs.ok
        state,_=state.observe(session,aci,obs)
    return state


def resolve():
    return action('RESOLVE_EDIT_INTENT',{'source_targets':['source.py'],
        'test_targets':['tests/test_source.py'],'relevant_facts':['Source and public tests observed.']})


def test_typed_intent_binds_verified_versions_and_original_responsibility(tmp_path,monkeypatch):
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    intent=resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
        source_targets=['source.py'],test_targets=['tests/test_source.py'])
    intent.validate_current(session,todo.work_contract)
    assert intent.source_targets==('source.py',)
    assert len(json.dumps(intent.to_dict()).encode())<8192
    assert 'value = 1' not in json.dumps(intent.to_dict())
    with pytest.raises(Exception):intent.objective='different'


@pytest.mark.parametrize('sources,tests,category',[
    (['unknown.py'],['tests/test_source.py'],'DISCOVERY_INCOMPLETE'),
    (['source.py'],[],'DISCOVERY_INCOMPLETE'),
    (['source.py','another.py'],['tests/test_source.py'],'STANDARD_REQUIRED'),
    (['tests/test_source.py'],['source.py'],'DISCOVERY_INCOMPLETE')])
def test_invalid_responsibility_never_resolves(tmp_path,monkeypatch,sources,tests,category):
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    with pytest.raises(DiscoveryError) as error:
        resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
            source_targets=sources,test_targets=tests)
    assert error.value.category==category


def test_stale_intent_denies_mutation(tmp_path,monkeypatch):
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    intent=resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
        source_targets=['source.py'],test_targets=['tests/test_source.py'])
    (tmp_path/'source.py').write_text('value = 9\n')
    with pytest.raises(DiscoveryError,match='version changed') as error:
        intent.validate_current(session,todo.work_contract)
    assert error.value.category=='INTENT_STALE'


def test_revoked_authorization_not_granted_by_old_observation(tmp_path,monkeypatch):
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    session=replace(session,policy=replace(session.policy,forbidden_paths=('source.py',)))
    with pytest.raises(DiscoveryError):
        resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
            source_targets=['source.py'],test_targets=['tests/test_source.py'])


def test_schema_offers_no_edit_run_or_unobserved_resolve(tmp_path,monkeypatch):
    _,_,_,_,_,session,state=setup(tmp_path,monkeypatch)
    assert {b['properties']['action']['const'] for b in discovery_schema(state)['oneOf']}=={'VIEW','SEARCH','FINISH'}
    state=viewed(state,session,'source.py','tests/test_source.py')
    schema=discovery_schema(state)
    assert {b['properties']['action']['const'] for b in schema['oneOf']}=={'RESOLVE_EDIT_INTENT','FINISH'}
    branch=next(b for b in schema['oneOf'] if b['properties']['action']['const']=='RESOLVE_EDIT_INTENT')
    assert branch['properties']['arguments']['properties']['source_targets']['items']['enum']==['source.py']


def test_real_discovery_native_handoff_roles_and_durable_replay(tmp_path,monkeypatch):
    owner,todo,store,executor,reports,_,_=setup(tmp_path,monkeypatch)
    editor=executor.values.copy()
    executor.values=[action('VIEW',{'path':''}),action('VIEW',{'path':'source.py'}),
        action('VIEW',{'path':'tests'}),action('VIEW',{'path':'tests/test_source.py'}),resolve(),*editor]
    captured=[];original=executor.execute
    def execute(request):captured.append(request);return original(request)
    executor.execute=execute
    kwargs=dict(profile_id='p',objective_with_context='RECOVERY_PROSE_MUST_NOT_REACH_EDITOR',attempt_index=1,fresh_workspace=True)
    result=owner.execute(todo,**kwargs)
    assert result.finalized and result.model_calls==7 and result.tool_calls==6
    assert len(reports)==2 and result.raw['execution_mode']=='SIMPLE_DISCOVERY_NATIVE_V1'
    ingress=json.loads(captured[0].messages[1].content)
    assert ingress['phase16_context']['bundle_id']
    assert ingress['phase16_context']['objective']==todo.work_contract.objective
    assert 'RECOVERY_PROSE_MUST_NOT_REACH_EDITOR' not in captured[0].messages[1].content
    assert [r.routing_decision.selected_model for r in captured[-2:]]==['qwen27_heavy_quality']*2
    assert 'RECOVERY_PROSE_MUST_NOT_REACH_EDITOR' not in '\n'.join(m.content for r in captured[-2:] for m in r.messages)
    assert (tmp_path/'source.py').read_text()=='value = 2\n'
    assert owner.execute(todo,**kwargs)==result and executor.calls==7
    states=[e.to_dict()['payload']['payload']['state'] for e in store.list_events('r')
        if e.to_dict()['payload'].get('evidence_type')=='INTERACTION_STATE_V1']
    assert not any(o['fresh'] and o['output'] for o in states[-1]['observations'] if o['path']=='source.py')


def test_native_failure_never_returns_to_explorer_edit(tmp_path,monkeypatch):
    owner,todo,store,executor,_,_,_=setup(tmp_path,monkeypatch)
    executor.values=[action('VIEW',{'path':'source.py'}),action('VIEW',{'path':'tests'}),action('VIEW',{'path':'tests/test_source.py'}),
        resolve(),native(count=0),native(count=0)]
    result=owner.execute(todo,profile_id='p',objective_with_context=todo.work_contract.objective,
        attempt_index=1,fresh_workspace=True)
    assert not result.finalized and executor.calls==6
    assert result.raw['native_failure_category']=='NATIVE_MUTATION_FAILED'
    assert (tmp_path/'source.py').read_text()=='value = 1\n'
    escalation=[e.to_dict()['payload'] for e in store.list_events('r')
        if e.to_dict()['payload'].get('evidence_type')=='SIMPLE_NATIVE_ESCALATION']
    assert len(escalation)==1 and escalation[0]['payload']['next_mode']=='STANDARD'


def test_discovery_restores_verified_views_without_reinspection(tmp_path,monkeypatch):
    _,todo,_,executor,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    executor.values=[resolve()]
    result=SimpleDiscoveryV1(executor=executor,observer=lambda *a,**kw:None,persist=lambda s:None,max_turns=3).execute(
        decision=decision(),contract=todo.work_contract,session=session,state=state,request_id='restart')
    assert result.intent and result.tool_calls==1 and result.model_calls==1


@pytest.mark.parametrize('bad_action',['EDIT','RUN'])
def test_explorer_rejects_mutating_action_even_if_model_ignores_schema(tmp_path,monkeypatch,bad_action):
    _,todo,_,executor,_,session,state=setup(tmp_path,monkeypatch)
    invalid=worker(json.dumps({'schema_version':1,'action':bad_action,'arguments':{},'summary':None}))
    executor.values=[invalid,invalid]
    result=SimpleDiscoveryV1(executor=executor,observer=lambda *a,**kw:None,persist=lambda s:None,max_turns=3).execute(
        decision=decision(),contract=todo.work_contract,session=session,state=state,request_id='invalid')
    assert result.intent is None and result.tool_calls==1 and result.failure=='DISCOVERY_PROTOCOL_EXHAUSTED'


def test_stale_handoff_does_not_call_editor(tmp_path,monkeypatch):
    owner,todo,_,executor,_,_,_=setup(tmp_path,monkeypatch)
    from harness.simple_worker_v2.edit_intent import TypedEditIntentV1
    original=TypedEditIntentV1.validate_current
    checks=[]
    def validate(intent,session,contract):
        checks.append(True)
        if len(checks)==2:(tmp_path/'source.py').write_text('value = 9\n')
        return original(intent,session,contract)
    monkeypatch.setattr(TypedEditIntentV1,'validate_current',validate)
    executor.values=[action('VIEW',{'path':'source.py'}),action('VIEW',{'path':'tests'}),action('VIEW',{'path':'tests/test_source.py'}),resolve()]
    result=owner.execute(todo,profile_id='p',objective_with_context=todo.work_contract.objective,
        attempt_index=1,fresh_workspace=True)
    assert not result.finalized and executor.calls==4 and result.raw['native_failure_category']=='INTENT_STALE'


def test_native_request_context_rechecks_intent_after_handoff_validation(tmp_path,monkeypatch):
    owner,todo,_,executor,_,_,_=setup(tmp_path,monkeypatch)
    original=owner.common.router._eligibility
    def eligibility(*args):
        (tmp_path/'source.py').write_text('value = 9\n')
        return original(*args)
    owner.common.router._eligibility=eligibility
    executor.values=[action('VIEW',{'path':'source.py'}),action('VIEW',{'path':'tests'}),action('VIEW',{'path':'tests/test_source.py'}),resolve()]
    result=owner.execute(todo,profile_id='p',objective_with_context=todo.work_contract.objective,
        attempt_index=1,fresh_workspace=True)
    assert not result.finalized and executor.calls==4 and result.raw['native_failure_category']=='INTENT_STALE'


def test_sufficient_discovery_rejects_more_repeated_views(tmp_path,monkeypatch):
    _,todo,_,executor,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    executor.values=[action('VIEW',{'path':'source.py'}),action('VIEW',{'path':'source.py'})]
    result=SimpleDiscoveryV1(executor=executor,observer=lambda *a,**kw:None,persist=lambda s:None,max_turns=3).execute(
        decision=decision(),contract=todo.work_contract,session=session,state=state,request_id='ready')
    assert result.tool_calls==1 and result.failure=='DISCOVERY_NO_PROGRESS'


def test_internal_alias_preserves_authorization_and_canonical_intent(tmp_path,monkeypatch):
    from test_workspace_canonical_authorization import link_directory
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    (tmp_path/'allowed').mkdir()
    (tmp_path/'allowed/other.py').write_text('value = 1\n')
    link_directory(tmp_path/'alias',tmp_path/'allowed')
    state=viewed(state,session,'alias/other.py','tests/test_source.py')
    intent=resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
        source_targets=['alias/other.py'],test_targets=['tests/test_source.py'])
    assert intent.source_versions[0][1]=='allowed/other.py'
    revoked=replace(session,policy=replace(session.policy,protected_paths=('allowed',)))
    with pytest.raises(Exception):intent.validate_current(revoked,todo.work_contract)


def test_intent_rejects_boolean_version_and_oversized_facts(tmp_path,monkeypatch):
    _,todo,_,_,_,session,state=setup(tmp_path,monkeypatch)
    state=viewed(state,session,'source.py','tests/test_source.py')
    intent=resolve_edit_intent(contract=todo.work_contract,session=session,state=state,
        source_targets=['source.py'],test_targets=['tests/test_source.py'])
    with pytest.raises(ValueError):replace(intent,schema_version=True)
    with pytest.raises(ValueError):replace(intent,relevant_facts=('x'*513,))
