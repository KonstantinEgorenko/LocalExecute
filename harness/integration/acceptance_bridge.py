"""Freeze public acceptance in existing RunStore before native mutation."""
from harness.control_plane import Evidence, EvidenceStatus
from harness.workspace import Workspace
from harness.workspace.errors import WorkspaceError
from harness.verification.acceptance import AcceptanceObligationsV1, build_acceptance_obligations, fingerprint


def load_frozen_acceptance(store, run_id, contract):
    matches = []
    for event in store.list_events(run_id):
        payload = event.to_dict()['payload']
        if payload.get('todo_id') == contract.todo_id and payload.get('evidence_type') == 'ACCEPTANCE_OBLIGATIONS_V1':
            frozen = AcceptanceObligationsV1.from_dict(payload['payload']['obligations'])
            if (frozen.run_id, frozen.todo_id, frozen.contract_fingerprint) != (run_id, contract.todo_id, fingerprint(contract.to_dict())):
                raise ValueError('frozen acceptance responsibility mismatch')
            if payload['payload']['fingerprint'] != fingerprint(frozen.to_dict()):
                raise ValueError('frozen acceptance integrity mismatch')
            matches.append(frozen)
    if matches and any(m != matches[0] for m in matches):
        raise ValueError('candidate cannot rewrite frozen acceptance')
    from .execution_responsibility import load_acceptance_lineage
    inherited = load_acceptance_lineage(store, run_id, contract)
    if matches and inherited is not None:
        raise ValueError('execution cannot replace inherited root acceptance')
    return matches[0] if matches else inherited


def freeze_acceptance(*, store, run_id, contract, session, sources, tests):
    previous = load_frozen_acceptance(store, run_id, contract)
    if previous is not None:
        return previous
    workspace = Workspace(session)
    if store.get_run(run_id).quota_usage.worker_calls:
        frozen = build_acceptance_obligations(run_id=run_id, contract=contract,sources={},tests={},
            unresolved=('Missing initial pre-worker snapshot; current files cannot retroactively establish public baseline',))
    else:
        frozen = build_acceptance_obligations(run_id=run_id, contract=contract,
            sources={p:workspace.read_text(p)[1] for p in sources},
            tests={p:workspace.read_text(p)[1] for p in tests})
    store.record_evidence(run_id, Evidence(f'{run_id}:acceptance:frozen', contract.todo_id,
        'ACCEPTANCE_OBLIGATIONS_V1', 'AcceptanceBridge', EvidenceStatus.PASSED,
        {'obligations':frozen.to_dict(), 'fingerprint':fingerprint(frozen.to_dict()),
         'boundary':'BEFORE_NATIVE_SOURCE', 'generated_tests_are_independent':False}))
    return frozen


def freeze_initial_acceptance(*, store, run_id, contract, session, interpreter=None, accepted_dependency=None):
    """Shared production worktree creation boundary, before any worker action.
    Bounded public inventory only. Oversized/unsupported evidence fails closed
    at Judge; it must not turn a partial snapshot into semantic certainty.
    """
    previous = load_frozen_acceptance(store, run_id, contract)
    if previous is not None:
        if accepted_dependency is not None:
            expected=accepted_dependency.to_dict()
            bindings=[__import__('json').loads(e.content).get('accepted_revision') for e in previous.evidence
                      if e.evidence_id.startswith('dependency_interface:')]
            if bindings!=[expected]:
                raise ValueError('PUBLIC_AUTHORITY_ACCEPTED_DEPENDENCY_MISMATCH: frozen execution lineage')
        return previous
    if store.get_run(run_id).quota_usage.worker_calls:
        return freeze_acceptance(store=store,run_id=run_id,contract=contract,session=session,sources=(),tests=())
    from harness.repo_intelligence.scanner import _kind
    from harness.repo_intelligence.models import RepositoryFileKind
    workspace = Workspace(session)
    sources, tests, docs = {}, {}, {}
    unresolved = ()
    authority_paths = None
    from harness.verification.callable_authority import AUTHORITY,INGRESS,PublicCallableAuthorityV1,public_read_projection
    if AUTHORITY in contract.constraints:
        raw=contract.to_dict()['constraints'][AUTHORITY]
        PublicCallableAuthorityV1(__import__('json').dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',',':')))
        if contract.constraints.get(INGRESS)!=raw['fingerprint']:
            raise ValueError('PUBLIC_AUTHORITY_INGRESS_MISMATCH')
        paths=(*raw['original_sources'],*(r['path'] for r in raw['public_requirements'].values()
            if r['provenance']=='PUBLIC_CONTRACT_EVIDENCE'))
        workspace=public_read_projection(session,paths)
        authority_paths=set(paths)
    try:
        if authority_paths is None:
            entries = workspace.list_files('', recursive=True)
            paths = sorted(e['path'] for e in entries if e['type'] == 'file'
                and (e['path'].endswith('.py') or e['path'].lower().endswith(('.md','.rst','.txt'))))
        else:
            # A file-scoped Todo need not authorize listing the repository root.
            # Enumerate only caller-declared public paths and this Todo's scope.
            for scope in session.policy.allowed_paths:
                _,target=workspace._candidate(scope)
                if target.is_file():authority_paths.add(scope)
                elif target.is_dir():
                    authority_paths.update(e['path'] for e in workspace.list_files(scope,recursive=True) if e['type']=='file')
            paths=sorted(p for p in authority_paths if p.endswith('.py') or p.lower().endswith(('.md','.rst','.txt')))
        if len(paths) > 16:
            raise ValueError('public acceptance context exceeds bounded file count')
        for path in paths:
            text = workspace.read_text(path)[1]
            if sum(len(t.encode('utf-8')) for group in (sources,tests,docs) for t in group.values()) + len(text.encode('utf-8')) > 32768:
                raise ValueError('public acceptance context exceeds 32 KiB')
            target = docs if not path.endswith('.py') else tests if _kind(path) is RepositoryFileKind.TEST else sources
            target[path] = text
    except (ValueError, OSError, WorkspaceError) as exc:
        sources, tests, docs = {}, {}, {}
        unresolved = ('Pre-mutation public context incomplete: ' + str(exc)[:512],)
    try:
        frozen = build_acceptance_obligations(run_id=run_id, contract=contract, sources=sources,
            tests=tests, public_contracts=docs, unresolved=unresolved,accepted_dependency=accepted_dependency)
    except ValueError as exc:
        from harness.verification.callable_authority import AUTHORITY
        if AUTHORITY in contract.constraints:
            raise
        frozen = build_acceptance_obligations(run_id=run_id, contract=contract, sources={},tests={},
            unresolved=(*unresolved,'Public acceptance snapshot unsupported: '+str(exc)[:512]))
    if interpreter is not None and not frozen.unresolved_semantics:
        frozen = interpreter(frozen)
    from harness.verification.callable_authority import AUTHORITY
    if AUTHORITY in contract.constraints and (frozen.unresolved_semantics or not frozen.public_interpretations
            or any(r.unresolved or r.input_validation is None or r.input_validation.status != 'VALID'
                   for r in frozen.public_interpretations)):
        store.record_evidence(run_id,Evidence(f'{run_id}:acceptance:qualification-failed',contract.todo_id,
            'PUBLIC_AUTHORITY_QUALIFICATION_FAILED','AcceptanceBridge',EvidenceStatus.FAILED,
            {'obligations':frozen.to_dict(),'boundary':'BEFORE_WORKER_EXECUTION'}))
        raise ValueError('PUBLIC_AUTHORITY_MANDATORY_QUALIFICATION_FAILED')
    store.record_evidence(run_id, Evidence(f'{run_id}:acceptance:frozen', contract.todo_id,
        'ACCEPTANCE_OBLIGATIONS_V1', 'AcceptanceBridge', EvidenceStatus.PASSED,
        {'obligations':frozen.to_dict(), 'fingerprint':fingerprint(frozen.to_dict()),
         'boundary':'BEFORE_WORKER_EXECUTION', 'generated_tests_are_independent':False}))
    return frozen
