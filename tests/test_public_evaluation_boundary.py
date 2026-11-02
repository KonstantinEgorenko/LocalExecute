"""Public completion does not import the optional private evaluator."""
import importlib
from types import SimpleNamespace

import pytest

from harness.integration import external_evaluation as boundary
from harness.integration.workflow_production_adapters import ProductionCompletionAdapter
from harness.verification.global_contract import GlobalVerificationError


def test_missing_mandatory_backend_is_typed_and_closed(monkeypatch):
    original = importlib.import_module

    def absent(name, *args, **kwargs):
        if name == 'harness.integration.independent_acceptance':
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, 'import_module', absent)
    with pytest.raises(GlobalVerificationError) as error:
        boundary.independent_verifier_type()
    assert error.value.code == 'INDEPENDENT_EVALUATOR_UNAVAILABLE'


def test_ordinary_completion_never_resolves_private_backend(monkeypatch):
    def forbidden():
        pytest.fail('Ordinary Mission imported private evaluator')

    monkeypatch.setattr(boundary, 'independent_verifier_type', forbidden)
    spine = SimpleNamespace(
        root_context=lambda _: None,
        latest_project_snapshot=lambda _: (
            SimpleNamespace(dirty_paths=(), head_commit='a' * 40),
            SimpleNamespace(artifact_id='snapshot')),
        verified_outcome=lambda *_: (None, SimpleNamespace(artifact_id='verified')),
    )
    adapter = ProductionCompletionAdapter(
        spine=spine, scheduler=SimpleNamespace(all_succeeded=lambda _: True))
    spec = SimpleNamespace(goal_id='goal', global_constraints={})
    checkpoint = SimpleNamespace(
        plan=SimpleNamespace(todo_ids=('todo',)), completed_todo_ids=('todo',))
    gate = adapter.evaluate(spec, checkpoint, SimpleNamespace(
        outcome=SimpleNamespace(value='SUCCEEDED')))
    assert gate.passed
    assert adapter.revalidate_completed(spec, checkpoint) is None
    assert adapter.finalize_summary(spec, 'summary', gate) == 'summary'


@pytest.mark.parametrize('constraints', [{boundary.INDEPENDENT_ACCEPTANCE: {}},
                                       {boundary.INDEPENDENT_ACCEPTANCE: False}])
def test_explicit_contract_presence_remains_mandatory(constraints):
    assert boundary.independent_acceptance_required(
        SimpleNamespace(), SimpleNamespace(global_constraints=constraints))


def test_corrupt_original_admission_cannot_drop_requirement():
    spine = SimpleNamespace(artifact_store=SimpleNamespace(),
                            root_context=lambda _: (_ for _ in ()).throw(ValueError('corrupt')))
    assert boundary.independent_acceptance_required(
        spine, SimpleNamespace(global_constraints={}, goal_id='goal'))


def test_missing_mandatory_evaluator_invalidates_terminal_restart(monkeypatch):
    from harness.mission.models import GlobalCompletionGateV1
    gate = GlobalCompletionGateV1(True, True, True, True, True, True, True, (), 'accepted')
    def absent():
        raise GlobalVerificationError('INDEPENDENT_EVALUATOR_UNAVAILABLE', 'not installed')
    monkeypatch.setattr(boundary, 'independent_verifier_type', absent)
    adapter = ProductionCompletionAdapter(spine=SimpleNamespace(),
        scheduler=SimpleNamespace(bind_spec=lambda _: None))
    spec = SimpleNamespace(global_constraints={boundary.INDEPENDENT_ACCEPTANCE: {}})
    result = adapter.revalidate_completed(spec, SimpleNamespace(completion_gate=gate))
    assert not result.passed
    assert 'INDEPENDENT_EVALUATOR_UNAVAILABLE' in result.summary
    with pytest.raises(RuntimeError, match='REQUIRES_ACCEPTANCE'):
        adapter.finalize_summary(spec, 'not accepted', result)

