"""Generic public dependent-module draft, without private benchmark assets."""
from pathlib import Path
import subprocess

from tests.test_replan_responsibility_lifecycle import setup, amend
from harness.integration.execution_responsibility import evidence_payloads


def test_public_multifile_draft_survives_fresh_lease(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    target = Path(w.source_repo_root)
    for name, text in {'producer.py': 'def values():\n    return []\n',
                       'consumer.py': 'from producer import values\ndef current():\n    return values()\n'}.items():
        (target/name).write_text(text, encoding='utf8')
    for args in [('add', '.'), ('-c', 'commit.gpgsign=false', 'commit', '-m', 'public module boundary')]:
        subprocess.run(['git', '-C', str(target), *args], check=True, capture_output=True)
    old, lease = w.ensure(todo)
    source = Path(w.manager.get_active(lease).worktree_root)
    draft = {name: (source/name).read_bytes()+b'\n# authorized unfinished draft\n'
             for name in ('producer.py', 'consumer.py')}
    for name, data in draft.items(): (source/name).write_bytes(data)
    new, revised_lease = w.ensure(amend(todo, scheduler, 'Update both modules with integration coverage.'))
    child = Path(w.manager.get_active(revised_lease).worktree_root)
    assert old != new and lease != revised_lease
    assert all((child/name).read_bytes() == data for name, data in draft.items())
    assert evidence_payloads(w.run_store, new, 'REPLAN_DRAFT_APPLIED_V1')[0]['parent_lease_id'] == lease
    assert not evidence_payloads(w.run_store, new, 'VERIFICATION_REPORT')
