from types import SimpleNamespace as NS
import pytest
from test_native_transport_recovery import fixture
from harness.integration.native_transport_recovery import NativeTransportCheckpoint,NativeRecoveryError


def failed_test(ledger):
    versions=ledger.versions(('source.py','tests/test_source.py'))
    source={'status':'PROVISIONAL_SOURCE_PASS','calls':1,'changed_paths':['source.py'],
            'report':{'outcome':'PASSED','results':[]}}
    ledger.put('SOURCE:PASS',result=source,versions=versions)
    ledger.put('TEST:FAILED',category='VERIFY_FAIL',versions=versions,
        result={'status':'FAILED','calls':2,'changed_paths':[],
                'failure':{'category':'VERIFY_FAIL'},'report':{'outcome':'FAILED','results':[]}})


def reopen(ledger,session,todo):
    return NativeTransportCheckpoint(store=ledger.store,session=session,contract=todo.work_contract,
                                    lease_id='lease',model=ledger.binding['model'])


def test_semantic_test_recovery_retains_applied_source_without_rediscovery(tmp_path,monkeypatch):
    _,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,[])
    failed_test(ledger)
    fresh=reopen(ledger,session,todo)
    intent=fresh.resume_intent()
    assert intent is not None and intent.source_targets==('source.py',)
    cached,_=fresh.stage_start('SOURCE',('source.py','tests/test_source.py'))
    assert cached['calls']==0 and cached['requires_fresh_final_verification'] is True
    assert not requests


def test_unchanged_candidate_and_failure_cannot_reopen_forever(tmp_path,monkeypatch):
    _,ledger,session,todo,_,_=fixture(tmp_path,monkeypatch,[])
    failed_test(ledger)
    fresh=reopen(ledger,session,todo)
    versions=fresh.versions(('source.py','tests/test_source.py'))
    fresh.put('TEST:FAILED',category='VERIFY_FAIL',versions=versions,
        result={'failure':{'category':'VERIFY_FAIL'},'report':{'outcome':'FAILED','results':[]}})
    with pytest.raises(NativeRecoveryError,match='NO_PROGRESS'):
        reopen(fresh,session,todo)


def test_new_semantic_progress_remains_eligible(tmp_path,monkeypatch):
    _,ledger,session,todo,_,_=fixture(tmp_path,monkeypatch,[])
    failed_test(ledger); fresh=reopen(ledger,session,todo)
    (tmp_path/'tests/test_source.py').write_text('def test_changed():\n    assert 3 == 3\n')
    fresh.put('TEST:FAILED',category='VERIFY_FAIL',versions=fresh.versions(('source.py','tests/test_source.py')),
        result={'failure':{'category':'VERIFY_FAIL'},'report':{'outcome':'FAILED','results':[]}})
    later=reopen(fresh,session,todo)
    assert later.generation==fresh.generation+1


def test_source_drift_prevents_false_applied_source_reuse(tmp_path,monkeypatch):
    _,ledger,session,todo,_,_=fixture(tmp_path,monkeypatch,[])
    failed_test(ledger)
    (tmp_path/'source.py').write_text('value = 99\n')
    with pytest.raises(NativeRecoveryError,match='STALE'):
        reopen(ledger,session,todo)


def test_no_progress_does_not_dispatch_another_diagnosis_or_replan(tmp_path):
    from test_replan_responsibility_lifecycle import setup
    from harness.integration.workflow_production_adapters import WorkflowRecoveryAdapter
    from harness.workflow.models import ExecutionObservationV1
    w,todo,_=setup(tmp_path);w.ensure(todo)
    calls=[]
    recovery=WorkflowRecoveryAdapter(bridge=NS(recover=lambda *a,**kw:calls.append('replan')),
        critic=NS(review=lambda **kw:calls.append('critic')),workspace=w).recover(todo,
        objective=todo.objective,execution=ExecutionObservationV1('p',False,(),0,0,'NEEDS_ATTENTION',
            'NO_PROGRESS',{'native_failure_category':'NO_PROGRESS'}),verification=None,
        failed_profile_ids=(),recovery_cycle=1)
    assert not recovery.continue_work and recovery.raw['cause_code']=='NO_PROGRESS'
    assert calls==[]
