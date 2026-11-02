from pathlib import Path
from dataclasses import replace
import pytest
from test_replan_responsibility_lifecycle import setup, amend, restart
from harness.integration.execution_responsibility import evidence_payloads

@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_fresh_replan_preserves_exact_source_and_untracked_test(tmp_path, newline):
    w, todo, scheduler = setup(tmp_path)
    old, lid = w.ensure(todo)
    lease = w.manager.get_active(lid)
    source = b'def f(x):' + newline + b'    return x + 1' + newline
    tests = b'from base import f' + newline + b'def test_edge():' + newline + b'    assert f(12)==13' + newline
    (Path(lease.worktree_root) / 'base.py').write_bytes(source)
    (Path(lease.worktree_root) / 'test_new.py').write_bytes(tests)
    revised = amend(todo, scheduler, 'Update implementation and preserve regression coverage.')
    new, newlid = w.ensure(revised)
    child = w.manager.get_active(newlid)
    assert newlid != lid and new != old
    assert (Path(child.worktree_root) / 'base.py').read_bytes() == source
    assert (Path(child.worktree_root) / 'test_new.py').read_bytes() == tests
    binding = evidence_payloads(w.run_store, new, 'REPLAN_DRAFT_APPLIED_V1')[0]
    assert binding['status'] == 'DRAFT_UNVERIFIED' and binding['parent_lease_id'] == lid
    assert not evidence_payloads(w.run_store, new, 'VERIFICATION_REPORT')
    assert restart(w).ensure(revised) == (new, newlid)
    assert len(evidence_payloads(w.run_store, new, 'REPLAN_DRAFT_APPLIED_V1')) == 1

def test_replan_forbidden_mutation_is_not_transferred(tmp_path):
    w, todo, s = setup(tmp_path)
    todo = replace(todo, work_contract=replace(todo.work_contract, forbidden_scope=('private',)))
    _, lid = w.ensure(todo)
    root = Path(w.manager.get_active(lid).worktree_root)
    (root / 'private').mkdir()
    (root / 'private/key.py').write_text('unauthorized=True\n')
    revised = amend(todo, s)
    with pytest.raises(ValueError, match='(forbidden|authority|draft|authorized)'):
        w.ensure(revised)
    assert (root / 'private/key.py').is_file()

def test_replan_scope_change_blocks_transfer(tmp_path):
    w, todo, s = setup(tmp_path)
    _, lid = w.ensure(todo)
    (Path(w.manager.get_active(lid).worktree_root) / 'base.py').write_bytes(b'changed\n')
    revised = amend(todo, s)
    revised = replace(revised, work_contract=replace(revised.work_contract, allowed_scope=('other.py',)))
    with pytest.raises(ValueError, match='(scope|authority|draft)'):
        w.ensure(revised)
