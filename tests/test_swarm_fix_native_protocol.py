from dataclasses import replace
import pytest
from test_edit_transport_experiment import native
from test_native_transport_recovery import fixture,test_stage
from harness.errors import ExecutionHttpError


def test_length_stopped_partial_call_has_typed_bounded_compact_recovery(tmp_path,monkeypatch):
    partial=replace(native(),tool_calls=(),done_reason='length')
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                   'replacement_lines':['    assert 2 == 2']})
    engine,ledger,session,_,requests,events=fixture(tmp_path,monkeypatch,[partial,change])
    result=test_stage(engine,session)
    assert result['status']=='PHASE7_PASS' and len(requests)==2
    rejected=[p for k,p in events if k=='EDIT_REJECTED']
    assert rejected[0]['protocol_failure_kind']=='OUTPUT_LIMIT_REACHED'
    assert 'TOOL_RECOVERY_STATE' in requests[1].messages[-1].content
    contracts=[p for k,p in events if k=='EDIT_WIRE_CONTRACT']
    assert contracts[-1]['tools'][0]['function']['parameters']['properties']['replacement_lines']['maxItems']==32


def test_json_eof_http_error_preserves_typed_cause(tmp_path,monkeypatch):
    error=ExecutionHttpError('invalid args',status_code=500,
        response_error_kind='TOOL_CALL_PROTOCOL_ERROR',protocol_error_reason='JSON_EOF')
    from types import SimpleNamespace as NS
    from harness.executor.models import ExecutionStage
    error.failure_context=NS(stage=ExecutionStage.GENERATION)
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                   'replacement_lines':['    assert 2 == 2']})
    engine,ledger,session,_,requests,events=fixture(tmp_path,monkeypatch,[error,change])
    result=test_stage(engine,session)
    assert result['status']=='PHASE7_PASS'
    rejected=[p for k,p in events if k=='EDIT_REJECTED']
    assert rejected[0]['protocol_failure_kind']=='TRUNCATED_TOOL_CALL'


def test_truncated_argument_exhaustion_cannot_reopen_a_fresh_generation(tmp_path,monkeypatch):
    from harness.integration.native_transport_recovery import NativeTransportCheckpoint,NativeRecoveryError
    bad=replace(native(),tool_calls=({'function':{'name':'apply_edit','arguments':'{"file":"tests/test_source.py"'}},))
    engine,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,[bad,bad])
    before=(tmp_path/'tests/test_source.py').read_bytes()
    result=test_stage(engine,session)
    assert result['status']=='FAILED' and len(requests)==2
    reopened=NativeTransportCheckpoint(store=ledger.store,session=session,contract=todo.work_contract,
                                      lease_id='lease',model=ledger.binding['model'])
    assert reopened.generation==ledger.generation
    with pytest.raises(NativeRecoveryError,match='AMBIGUOUS'):
        reopened.stage_start('TEST',('source.py','tests/test_source.py'))
    assert (tmp_path/'tests/test_source.py').read_bytes()==before
