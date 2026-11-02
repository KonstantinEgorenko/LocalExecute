import json
from dataclasses import replace
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from harness.edit_transport_experiment import diff_edit_intent, native_edit_intent, diagnostic_wire, EditResponsibilityExperiment, EditIntentError
from harness.workspace import WorkspacePolicy, WorkspaceSession
from harness.workspace_aci_v2 import WorkspaceAciV2
from harness.executor.models import ExecutionOutcome
from test_simple_worker_v2 import action, decision

def patch(path='target.py', old='value = 1', new='value = 2'):
    return f'*** Begin Patch\n*** Update File: {path}\n@@\n-{old}\n+{new}\n*** End Patch'

@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_diff_exact_context_and_newline(newline):
    source = 'value = 1' + newline + 'other = 3' + newline
    a = diff_edit_intent(patch(), {'target.py': source})['target.py']
    assert a.expected_observed_content == source
    assert a.new_text == 'value = 2' + newline + 'other = 3' + newline

@pytest.mark.parametrize('raw', [patch(old='value = 9'), patch(path='../target.py'), patch(path='C:/target.py'), patch().replace('@@', '@@ -1,1 +1,1 @@'), patch().replace('*** End Patch', ''), patch().replace('-value = 1', '- value = 1'), patch().replace('*** End Patch', '@@\n-value = 1\n+value = 3\n*** End Patch')])
def test_diff_rejects_stale_context_paths_malformed_overlaps(raw):
    with pytest.raises(ValueError):
        diff_edit_intent(raw, {'target.py': 'value = 1\n'})

def test_ambiguous_context_rejected():
    with pytest.raises(EditIntentError):
        diff_edit_intent(patch(), {'target.py': 'value = 1\nvalue = 1\n'})

def test_multi_file_separation_and_all_contexts_validated():
    raw = patch().replace('*** End Patch', '*** Update File: tests/test_target.py\n@@\n-assert value == 1\n+assert value == 2\n*** End Patch')
    sources = {'target.py': 'value = 1\n', 'tests/test_target.py': 'assert value == 1\n'}
    actions = diff_edit_intent(raw, sources)
    assert set(actions) == set(sources)
    assert actions['tests/test_target.py'].new_text == 'assert value == 2\n'
    with pytest.raises(EditIntentError):
        diff_edit_intent(raw.replace('-assert value == 1', '-assert value == 9'), sources)

def native(args=None, name='apply_edit', count=1):
    args = args if args is not None else {'file': 'target.py', 'start_line': 1, 'end_line': 1, 'replacement_lines': ['value = 2']}
    return replace(action('FINISH', {}, 'x'), content='', tool_calls=tuple(({'function': {'name': name, 'arguments': args}} for _ in range(count))))

@pytest.mark.parametrize('result', [native(count=0), native(count=2), native(name='RUN_COMMAND'), native({'file': '../target.py', 'start_line': 1, 'end_line': 1, 'replacement_lines': ['x']}), native({'file': 'target.py', 'start_line': True, 'end_line': 1, 'replacement_lines': ['x']}), native({'file': 'target.py', 'start_line': 1, 'end_line': 1, 'replacement_lines': 'x'})])
def test_native_fails_closed(result):
    with pytest.raises(ValueError):
        native_edit_intent(result, {'target.py': 'value = 1\n'})

def test_native_arguments_channel_not_content_json():
    a = native_edit_intent(native(), {'target.py': 'value = 1\n'})['target.py']
    assert a.new_text == 'value = 2\n'
    with pytest.raises(EditIntentError):
        native_edit_intent(replace(native(count=0), content=json.dumps({'name': 'apply_edit'})), {'target.py': 'value = 1\n'})

def test_native_ollama_index_metadata_does_not_change_edit_arguments():
    result = native()
    call = dict(result.tool_calls[0])
    call['function'] = {**call['function'], 'index': 0}
    result = replace(result, tool_calls=(call,))
    assert native_edit_intent(result, {'target.py': 'value = 1\n'})['target.py'].new_text == 'value = 2\n'
    call['function']['index'] = True
    with pytest.raises(EditIntentError):
        native_edit_intent(result, {'target.py': 'value = 1\n'})

def test_joint_multiple_files_rejected_before_any_application(tmp_path):
    sources = {'target.py': 'value = 1\n', 'test_target.py': 'assert value == 1\n'}
    for path, text in sources.items():
        (tmp_path / path).write_text(text)
    raw = patch().replace('*** End Patch', '*** Update File: test_target.py\n@@\n-assert value == 1\n+assert value == 2\n*** End Patch')
    result = EditResponsibilityExperiment(NS(execute=lambda _: replace(action('FINISH', {}, 'x'), content=raw)), NS(_request=lambda *_a, **_k: None), aci=NS(execute=lambda **_: pytest.fail('no partial application'))).execute(decision=decision(), variant='diff', split=False, objective='Update behavior and tests', session=WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy()), source_paths=('target.py',), test_paths=('test_target.py',), verify=lambda _: pytest.fail('no verification'))
    assert result['outcome'] == 'FAILED' and result['model_calls'] == 2
    assert 'MULTI_FILE_ATOMIC_UNAVAILABLE' in result['stages'][0]['failure']['detail']
    assert all(((tmp_path / path).read_text() == text for path, text in sources.items()))

def test_native_wire_is_real_tools_not_format_and_restores_client():
    seen = []

    def request(*args, **kwargs):
        seen.append(kwargs['payload'])
        return {}
    client = NS(_request=request)
    with diagnostic_wire(client, 'native', {'target.py': 'value = 1\n'}, lambda *_: None):
        client._request('endpoint', '/api/chat', timeout_seconds=1, payload={'options': {}})
    assert client._request is request
    assert seen[0]['tools'][0]['function']['name'] == 'apply_edit' and 'format' not in seen[0]
    assert seen[0]['think'] is False

def test_diff_cas_and_syntax_preflight_remain_authoritative(tmp_path):
    source = 'value = 1\n'
    target = tmp_path / 'target.py'
    target.write_text(source)
    session = WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy())
    invalid = diff_edit_intent(patch(new='value = ('), {'target.py': source})['target.py']
    result = WorkspaceAciV2().execute(session=session, call_id='syntax', action=invalid)
    assert not result.ok and target.read_text() == source
    valid = diff_edit_intent(patch(), {'target.py': source})['target.py']
    target.write_text('value = 99\n')
    result = WorkspaceAciV2().execute(session=session, call_id='stale', action=valid)
    assert not result.ok and target.read_text() == 'value = 99\n'

def test_split_fresh_context_targets_tests_and_requires_final_verification(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    (tmp_path / 'test_target.py').write_text('def test_value():\n    assert value == 1\n')
    requests = []
    verified = []

    def execute(req):
        requests.append(req)
        text = patch() if len(requests) == 1 else patch('test_target.py', '    assert value == 1', '    assert value == 2')
        return replace(action('FINISH', {}, 'x'), content=text)
    client = NS(_request=lambda *_args, **_kw: None)

    def verify(stage):
        verified.append(stage)
        return {'outcome': 'PASSED' if stage == 'SOURCE' else 'FAILED', 'results': [{'gate_id': 'regression_change', 'outcome': 'FAILED'}]}
    result = EditResponsibilityExperiment(NS(execute=execute), client).execute(decision=decision(), variant='diff', split=True, objective='Update value and regression coverage', session=WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy()), source_paths=('target.py',), test_paths=('test_target.py',), verify=verify)
    assert result['outcome'] == 'FAILED' and result['stages'][0]['status'] == 'PROVISIONAL_SOURCE_PASS'
    assert verified == ['SOURCE', 'TEST'] and len(requests) == 3
    assert 'value = 2' in requests[1].messages[1].content
    assert json.dumps(['test_target.py']) in requests[1].messages[1].content
    assert '"kind": "ADD_REGRESSION_COVERAGE"' in requests[1].messages[1].content
    assert all((len(r.messages) == 2 and 'hidden' not in r.messages[1].content for r in requests))

def test_invalid_source_stops_before_test_stage(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    (tmp_path / 'test_target.py').write_text('pass\n')
    executor = NS(execute=lambda _: replace(action('FINISH', {}, 'x'), content=patch(new='value = (')))
    result = EditResponsibilityExperiment(executor, NS(_request=lambda *_a, **_k: None)).execute(decision=decision(), variant='diff', split=True, objective='Update value', session=WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy()), source_paths=('target.py',), test_paths=('test_target.py',), verify=lambda _: pytest.fail('invalid source'))
    assert result['model_calls'] == 2 and len(result['stages']) == 1

def test_test_responsibility_cannot_mutate_source_or_read_hidden_context(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    (tmp_path / 'test_target.py').write_text('assert value == 1\n')
    (tmp_path / 'private_acceptance.py').write_text('PRIVATE_ACCEPTANCE_SENTINEL\n')
    requests = []

    def execute(request):
        requests.append(request)
        return replace(action('FINISH', {}, 'x'), content=patch(new='value = 2' if len(requests) == 1 else 'value = 3', old='value = 1' if len(requests) == 1 else 'value = 2'))
    result = EditResponsibilityExperiment(NS(execute=execute), NS(_request=lambda *_a, **_k: None)).execute(decision=decision(), variant='diff', split=True, objective='Update behavior and regression coverage', session=WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy()), source_paths=('target.py',), test_paths=('test_target.py',), verify=lambda stage: {'outcome': 'PASSED', 'results': []})
    assert result['outcome'] == 'FAILED' and result['model_calls'] == 3
    assert result['stages'][1]['failure']['category'] == 'CONTRACT_BINDING_FAIL'
    assert (tmp_path / 'target.py').read_text() == 'value = 2\n'
    assert (tmp_path / 'test_target.py').read_text() == 'assert value == 1\n'
    assert all(('PRIVATE_ACCEPTANCE_SENTINEL' not in m.content and 'private_acceptance.py' not in m.content for request in requests for m in request.messages))

def test_failed_executor_result_cannot_apply_even_valid_content(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    failed = replace(action('FINISH', {}, 'x'), content=patch(), outcome=ExecutionOutcome.EXECUTION_FAILURE, error='failed admission')
    result = EditResponsibilityExperiment(NS(execute=lambda _: failed), NS(_request=lambda *_a, **_k: None)).execute(decision=decision(), variant='diff', split=False, objective='Update value', session=WorkspaceSession('s', 'r', 't', str(tmp_path), WorkspacePolicy()), source_paths=('target.py',), test_paths=(), verify=lambda _: pytest.fail('failed execution'))
    assert result['outcome'] == 'FAILED' and result['model_calls'] == 1
    assert (tmp_path / 'target.py').read_text() == 'value = 1\n'
