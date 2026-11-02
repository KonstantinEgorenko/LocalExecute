'Shared SOURCE/TEST primitive; the existing verification coordinator owns gates.'
from dataclasses import replace
from harness.edit_transport_experiment import EditResponsibilityExperiment


def execute_native_mutation(*, owner, todo, run_id, lease, session, decision,
                            executor, client, observe, objective, sources, tests, expected_initial_versions=(),
                            checkpoint=None):
    workspace = owner.legacy.workspace
    store = workspace.run_store
    contract = todo.work_contract
    from .source_repair import SourceRepairPolicy
    source_policy = SourceRepairPolicy.from_constraints(contract.constraints)
    from harness.execution_policy import MULTIFILE_SOURCE_LIMIT
    source_limit = contract.constraints.get(MULTIFILE_SOURCE_LIMIT, 1)
    if type(source_limit) is not int or source_limit not in (1, 2):
        raise ValueError('invalid bounded multi-file source limit')
    from harness.verification.test_contract import policy as test_policy, committed_files, test_files, inspect_tests
    test_contract = test_policy(contract.constraints)
    baseline = test_files(committed_files(owner.verification.runner,
        __import__('pathlib').Path(lease.worktree_root), workspace.manager.snapshot(lease.lease_id))) if test_contract else {}
    from .acceptance_bridge import freeze_acceptance
    from harness.verification.acceptance import canonical
    frozen = freeze_acceptance(store=store, run_id=run_id, contract=contract,
        session=session, sources=sources, tests=tests)
    observe('ACCEPTANCE_OBLIGATIONS_FROZEN', {'obligations':frozen.to_dict()})
    editor_acceptance = frozen.to_dict()
    for interpretation in editor_acceptance['public_interpretations']:
        interpretation.pop('witness', None)
    objective = ('AUTHORITATIVE OBJECTIVE / CONSTRAINTS\n' + canonical(contract.to_dict())
        + '\n\nFROZEN ACCEPTANCE OBLIGATIONS / PRE-MUTATION PUBLIC EVIDENCE\n'
        + canonical(editor_acceptance)
        + '\n\nUNTRUSTED EXPLORER HYPOTHESES / ADDITIONAL WORKER CONTEXT\n' + objective
        + '\n\nOBSERVED SOURCE / TEST CONTEXT follows as untrusted implementation data. '
        'Implementation expressions are not intended-behavior specifications. '
        'Generated regression expectations must follow independent public obligations, '
        'not copy the candidate or a hypothesis. Unclear requirements remain unresolved.')
    def verify(responsibility):
        coordinator = owner.verification
        owner.legacy._reopen_for_slice(run_id)
        if responsibility == 'SOURCE':
            plan = coordinator.compiler.compile(run=store.get_run(run_id),
                todo=coordinator.run_store.get_todo(todo.todo_id), contract=contract, lease=lease)
            plan = replace(plan, gates=tuple(g for g in plan.gates if g.gate_id in
                {'candidate_diff', 'scope_guard', 'python_syntax', 'pytest', 'immutable_baseline_tests'}))
            return coordinator.runner.run(plan=plan, contract=contract).to_dict()
        return coordinator.verify(run_id=run_id, contract=contract, lease_id=lease.lease_id).report.to_dict()
    def test_evidence(objective, current, report, source_receipt, remaining, *, rejection=None, original_intent=None, binding=None):
        from .test_edit_safety import compact_context
        return compact_context(contract=contract.to_dict(),frozen=frozen.to_dict(),baseline={p:s for p,s in baseline.items() if p in tests},
            current=current,report=report,rejection=rejection,original_intent=original_intent,
            source_receipt=source_receipt,binding=binding,remaining=remaining)
    definition=owner.common.registry.get(decision.selected_model) if test_contract else None
    repair_profile=None if definition is None else {'model':definition.ollama_model,
        'num_ctx':definition.context['default'],'num_gpu':definition.runtime.get('num_gpu')}
    return EditResponsibilityExperiment(executor, client, observe, checkpoint=checkpoint,
        source_operation_limit=source_limit,
        source_policy=source_policy,
        test_evidence_provider=test_evidence if test_contract else None,
        protected_test_baseline=baseline if test_contract else None,
        protected_test_policy=test_contract,
        repair_request_profile=repair_profile,
        diff_provider=lambda: workspace.manager.snapshot(lease.lease_id).diff_text).execute(
        decision=decision, variant='native', split=True, objective=objective,
        session=session, source_paths=sources, test_paths=tests, verify=verify,
        expected_initial_versions=expected_initial_versions)
