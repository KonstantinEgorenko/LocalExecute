import json

import pytest

from harness.simple_worker_v2 import SimpleWorkerV2, SimpleWorkerOutcomeV2
from harness.simple_worker_v2.protocol import parse_simple_worker_action, SimpleWorkerProtocolError
from test_simple_worker_v2 import setup, Executor, action, decision, worker


def edit(text, first=1, last=1, path="target.py", create=False):
    args = {"file": path, "replacement": text}
    if not create:
        args["lines"] = {"first": first, "last": last}
    return worker(json.dumps({"schema_version": 1, "action": "EDIT", "arguments": args, "summary": None}))


def execute(tmp_path, values, executor_type=Executor):
    contract, bundle, session = setup(tmp_path, requires_mutation=True)
    executor = executor_type(values)
    result = SimpleWorkerV2(executor=executor).execute(decision=decision(), contract=contract,
        context_bundle=bundle, workspace_session=session)
    return result, executor


def test_compact_range_preserves_literal_multiline_source_and_boundaries(tmp_path):
    source = 'UNCHANGED = 1\ndef value():\n    return 1\nTAIL = 3\n'
    (tmp_path / 'target.py').write_text(source)
    text = '    text = r"C:\\temp"\n    return text'
    result, executor = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        edit(text, 3, 3), action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / 'target.py').read_text() == source.replace('    return 1', text)
    assert result.changed_paths == ('target.py',)
    for branch in executor.calls[1].response_format['oneOf']:
        if branch['properties']['action']['const'] == 'EDIT':
            fields = branch['properties']['arguments']['properties']
            assert set(fields) in ({'file', 'replacement'}, {'file', 'lines', 'replacement'})
            assert sorted(fields).index('file') < sorted(fields).index('replacement')
            assert source not in json.dumps(branch)


@pytest.mark.parametrize('source,text,last', [('value = 1\n', 'value = 2', 1),
    ('value = 1', 'value = 2', 1)])
def test_explicit_complete_observed_span_rewrite(tmp_path, source, text, last):
    (tmp_path / 'target.py').write_text(source)
    result, _ = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        edit(text, last=last), action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / 'target.py').read_text() == text + ('\n' if source.endswith('\n') and not text.endswith('\n') else '')


def test_compact_creation_cannot_replace_existing_file(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    result, _ = execute(tmp_path, [edit('value = 2\n', create=True)])
    assert result.changed_paths == ()
    assert (tmp_path / 'target.py').read_text() == 'value = 1\n'


def test_compact_new_file_creation(tmp_path):
    result, _ = execute(tmp_path, [edit('value = 2\n', create=True), action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS


@pytest.mark.parametrize('first,last', [(True, 1), (1.0, 1), (1, '1'), (0, 1), (2, 1), (1, 3)])
def test_compact_bounds_remain_strict(first, last):
    with pytest.raises(SimpleWorkerProtocolError):
        parse_simple_worker_action(edit('x', first, last).content, viewed_files={'target.py': 'x\ny\n'})


def test_compact_range_requires_current_view():
    with pytest.raises(SimpleWorkerProtocolError, match='current bounded VIEW'):
        parse_simple_worker_action(edit('x').content)


def test_compact_stale_view_is_not_overwritten(tmp_path):
    (tmp_path / 'target.py').write_text('value = 1\n')
    class Concurrent(Executor):
        def execute(self, request):
            result = super().execute(request)
            if len(self.calls) == 2:
                (tmp_path / 'target.py').write_text('value = 99\n')
            return result
    result, _ = execute(tmp_path, [action('VIEW', {'path': 'target.py'}), edit('value = 2')], Concurrent)
    assert result.changed_paths == ()
    assert (tmp_path / 'target.py').read_text() == 'value = 99\n'


def test_compact_rejected_syntax_is_not_repaired_or_written(tmp_path):
    source = 'def value():\n    return 1\n'
    (tmp_path / 'target.py').write_text(source)
    result, executor = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        edit('return 2)', 2, 2), edit('    return 2', 2, 2), action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / 'target.py').read_text() == source.replace('return 1', 'return 2')
    assert any('SYNTAX_PREFLIGHT_FAILED' in m.content for m in executor.calls[2].messages)


def test_compact_multiline_cannot_ignore_safer_line_contract(tmp_path):
    source = 'def value():\n    return 1\n'
    (tmp_path / 'target.py').write_text(source)
    result, _ = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        edit('def value():\n    return 2)', 1, 2), edit('    x = 2\n    return x', 2, 2)])
    assert result.reason == 'EDIT_CONTRACT_NOT_APPLIED'
    assert result.changed_paths == ()
    assert (tmp_path / 'target.py').read_text() == source


def test_compact_rejected_single_line_does_not_restrict_multiline_repair(tmp_path):
    source = 'def value():\n    return 1\n'
    (tmp_path / 'target.py').write_text(source)
    result, executor = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        edit('    return 2)', 2, 2), edit('    x = 2\n    return x', 2, 2),
        action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / 'target.py').read_text() == 'def value():\n    x = 2\n    return x\n'
    assert any('SYNTAX_PREFLIGHT_FAILED' in m.content for m in executor.calls[2].messages)
    assert all('REJECTED_MULTILINE_SYNTAX_PREFLIGHT' not in m.content
               for m in executor.calls[2].messages)


def test_large_file_view_keeps_native_exact_fragment_edit(tmp_path):
    source = '# untouched\n' * 129 + 'VALUE = 1\n'
    (tmp_path / 'target.py').write_text(source)
    fragment = worker(json.dumps({'schema_version': 1, 'action': 'EDIT', 'arguments': {
        'file': 'target.py', 'old_text': 'VALUE = 1', 'replacement': 'VALUE = 2'}, 'summary': None}))
    result, executor = execute(tmp_path, [action('VIEW', {'path': 'target.py'}),
        fragment, action('FINISH', {}, 'complete')])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / 'target.py').read_text() == source.replace('VALUE = 1', 'VALUE = 2')
    assert any('old_text' in b['properties']['arguments']['properties']
        for b in executor.calls[1].response_format['oneOf'] if b['properties']['action']['const'] == 'EDIT')


def test_verified_feedback_stays_bound_to_report_until_fresh_verification(tmp_path):
    (tmp_path / 'target.py').write_text('VALUE = 1\n')
    contract, bundle, session = setup(tmp_path, requires_mutation=True)
    executor = Executor([action('VIEW', {'path': 'target.py'}), edit('VALUE = 2'),
        action('FINISH', {}, 'complete')])
    feedback = {'report_id': 'report', 'failed_required_gates': [{'gate_id': 'regression_change'}]}
    result = SimpleWorkerV2(executor=executor).execute(decision=decision(), contract=contract,
        context_bundle=bundle, workspace_session=session, verification_feedback=feedback)
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    contracts = [json.loads(next(m.content.split('\n', 1)[1] for m in request.messages
        if m.content.startswith('ACTION_CONTRACT\n'))) for request in executor.calls]
    assert contracts[0]['verification_feedback']['snapshot_relation'] == 'BOUND_AT_EXECUTION_START'
    assert contracts[2]['verification_feedback']['snapshot_relation'] == 'PREVIOUS_CANDIDATE'
    assert contracts[2]['verification_feedback']['state'] == 'HISTORICAL_FAILED_CANDIDATE'
    assert all(c['verification_feedback']['failed_required_gates'] == feedback['failed_required_gates'] for c in contracts)
    assert feedback == {'report_id': 'report', 'failed_required_gates': [{'gate_id': 'regression_change'}]}
