import json

import pytest

from harness.simple_worker_v2 import SimpleWorkerV2, SimpleWorkerOutcomeV2
from harness.simple_worker_v2.protocol import parse_simple_worker_action, SimpleWorkerProtocolError
from test_simple_worker_v2 import setup, Executor, action, decision, worker


def range_edit(start, end, lines, path="target.py"):
    return worker(json.dumps({"schema_version": 1, "action": "EDIT", "arguments": {
        "match": {"kind": "RANGE", "file": {"path": path}, "start_line": start, "end_line": end},
        "replacement_lines": lines}, "summary": None}))


def run(tmp_path, values, requires_mutation=True, executor_type=Executor):
    contract, bundle, session = setup(tmp_path, requires_mutation=requires_mutation)
    executor = executor_type(values)
    result = SimpleWorkerV2(executor=executor).execute(decision=decision(), contract=contract,
        context_bundle=bundle, workspace_session=session)
    return result, executor


def test_view_then_range_binds_observed_source_and_preserves_literal_lines(tmp_path):
    source = '# untouched\ndef value():\n    return 1\n'
    (tmp_path / "target.py").write_text(source)
    replacement = ['    text = r"C:\\temp"', '    return text']
    result, executor = run(tmp_path, [action("VIEW", {"path": "target.py"}),
        range_edit(3, 3, replacement), action("FINISH", {}, "complete")])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / "target.py").read_text() == '# untouched\ndef value():\n' + '\n'.join(replacement) + '\n'
    assert any('3 |     return 1' in m.content for m in executor.calls[1].messages)
    assert result.changed_paths == ("target.py",)


def test_explicit_complete_range_replacement_remains_available(tmp_path):
    (tmp_path / "target.py").write_text('def old():\n    return 1\n')
    result, _ = run(tmp_path, [action("VIEW", {"path": "target.py"}),
        range_edit(1, 2, ['def new():', '    return 2']), action("FINISH", {}, "complete")])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / "target.py").read_text() == 'def new():\n    return 2\n'


def test_range_no_op_does_not_claim_mutation(tmp_path):
    (tmp_path / "target.py").write_text('value = 1\n')
    result, _ = run(tmp_path, [action("VIEW", {"path": "target.py"}),
        range_edit(1, 1, ['value = 1']), action("FINISH", {}, "unchanged")], requires_mutation=False)
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.changed_paths == ()


def test_stale_observed_source_is_not_overwritten(tmp_path):
    (tmp_path / "target.py").write_text('value = 1\n')
    class ConcurrentChange(Executor):
        def execute(self, request):
            value = super().execute(request)
            if len(self.calls) == 2:
                (tmp_path / "target.py").write_text('value = 99\n')
            return value
    result, _ = run(tmp_path, [action("VIEW", {"path": "target.py"}), range_edit(1, 1, ['value = 2'])],
                    executor_type=ConcurrentChange)
    assert result.outcome is SimpleWorkerOutcomeV2.NEEDS_ATTENTION
    assert result.changed_paths == ()
    assert (tmp_path / "target.py").read_text() == 'value = 99\n'


@pytest.mark.parametrize("start,end,lines", [(True, 1, ['x']), (1.0, 1, ['x']),
    (0, 1, ['x']), (2, 1, ['x']), (1, 3, ['x']), (1, 1, ['x\ny']),
    (1, 1, ['x\ry']), (1, 1, [True]), (1, 1, 'x')])
def test_range_rejects_invalid_bounds_and_non_literal_line_arrays(start, end, lines):
    with pytest.raises(SimpleWorkerProtocolError):
        parse_simple_worker_action(range_edit(start, end, lines).content, viewed_files={"target.py": 'x\ny\n'})


def test_range_requires_explicit_current_view():
    with pytest.raises(SimpleWorkerProtocolError, match="current bounded VIEW"):
        parse_simple_worker_action(range_edit(1, 1, ['x']).content)


def test_range_recovery_is_applied_after_rejected_multiline_proposal(tmp_path):
    old = 'def value():\n    return 1\n'
    (tmp_path / "target.py").write_text(old + '\nUNCHANGED = 4\n')
    bad = action("EDIT", {"kind": "FRAGMENT", "path": "target.py", "old_text": old,
        "new_text": old.replace('return 1', 'return 2)'), "expected_occurrences": 1})
    result, executor = run(tmp_path, [action("VIEW", {"path": "target.py"}), bad,
        range_edit(2, 2, ['    return 2']), action("FINISH", {}, "complete")])
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert (tmp_path / "target.py").read_text() == old.replace('return 1', 'return 2') + '\nUNCHANGED = 4\n'
    assert any('"tool_correction_required": true' in m.content for m in executor.calls[2].messages)


def test_invalid_line_array_presents_typed_bounded_correction_then_valid_edit(tmp_path):
    source = 'def value():\n    return 1\n'
    (tmp_path / "target.py").write_text(source)
    events = []
    contract, bundle, session = setup(tmp_path, requires_mutation=True)
    executor = Executor([action("VIEW", {"path": "target.py"}),
        range_edit(1, 2, ['def value():\n    return 2']),
        range_edit(2, 2, ['    return 2']), action("FINISH", {}, "complete")])
    result = SimpleWorkerV2(executor=executor, observer=lambda kind, payload: events.append((kind, payload))).execute(
        decision=decision(), contract=contract, context_bundle=bundle, workspace_session=session)
    assert result.outcome is SimpleWorkerOutcomeV2.SUCCESS
    assert result.aci_calls == 2  # VIEW and explicit corrective EDIT; no repaired action.
    assert (tmp_path / "target.py").read_text() == source.replace('return 1', 'return 2')
    failure = next(p for kind, p in events if kind == "PROTOCOL_ERROR")
    assert failure['correction']['error_code'] == 'INVALID_SOURCE_LINE_ARRAY'
    assert failure['correction']['workspace_mutated'] is False
    request = executor.calls[2]
    assert any(m.content.startswith('TOOL_CORRECTION_REQUIRED\n') for m in request.messages)
    branches = request.response_format['oneOf']
    assert {b['properties']['action']['const'] for b in branches} == {'EDIT', 'RUN'}
    target = next(b for b in branches if b['properties']['action']['const'] == 'EDIT'
        and 'lines' in b['properties']['arguments']['properties'])
    assert target['properties']['arguments']['properties']['replacement']['type'] == 'string'
    ranges = [b['properties']['arguments']['properties']['lines']['properties'] for b in branches
        if b['properties']['action']['const'] == 'EDIT'
        and 'lines' in b['properties']['arguments']['properties']]
    assert all(m['first']['enum'] == m['last']['enum'] for m in ranges)
    assert {m['first']['enum'][0] for m in ranges} == {1, 2}


@pytest.mark.parametrize('next_action', [action("VIEW", {"path": "target.py"}),
    range_edit(1, 2, ['def value():', '    return 2']),
    range_edit(2, 2, ['    value = 2', '    return value'])])
def test_ignored_invalid_line_array_correction_fails_before_mutation(tmp_path, next_action):
    source = 'def value():\n    return 1\n'
    (tmp_path / "target.py").write_text(source)
    result, _ = run(tmp_path, [action("VIEW", {"path": "target.py"}),
        range_edit(1, 2, ['def value():\n    return 2']), next_action])
    assert result.outcome is SimpleWorkerOutcomeV2.NEEDS_ATTENTION
    assert result.reason in ('TOOL_CORRECTION_NOT_APPLIED', 'EDIT_CONTRACT_NOT_APPLIED')
    assert result.aci_calls == 1
    assert result.changed_paths == ()
    assert (tmp_path / "target.py").read_text() == source
