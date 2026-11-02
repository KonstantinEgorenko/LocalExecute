"""Revision-scoped execution identity in the existing RunStore evidence ledger.

The scheduler amendment is generation authority; claim versions are not.
Root acceptance remains an immutable, separately referenced public snapshot.
"""
from dataclasses import dataclass, asdict
import json

from harness.control_plane import Evidence, EvidenceStatus
from harness.verification.acceptance import AcceptanceObligationsV1, fingerprint


class StaleResponsibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionResponsibilityV1:
    todo_id: str
    work_contract_fingerprint: str
    source_amendment_id: str | None
    amendment_version: int | None

    def __post_init__(self):
        if type(self.todo_id) is not str or not self.todo_id:
            raise ValueError('invalid responsibility Todo identity')
        if (type(self.work_contract_fingerprint) is not str
            or len(self.work_contract_fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in self.work_contract_fingerprint)):
            raise ValueError('invalid responsibility contract fingerprint')
        if self.source_amendment_id is not None and (
            type(self.source_amendment_id) is not str or not self.source_amendment_id):
            raise ValueError('invalid responsibility amendment identity')
        if self.amendment_version is not None and (
            type(self.amendment_version) is not int or self.amendment_version < 1):
            raise ValueError('invalid responsibility amendment version')

    def to_dict(self):
        return asdict(self)


def evidence_payloads(store, run_id, kind):
    return [event.to_dict()['payload']['payload'] for event in store.list_events(run_id)
            if event.to_dict()['payload'].get('evidence_type') == kind]


def record_once(store, run_id, todo_id, kind, payload):
    old = evidence_payloads(store, run_id, kind)
    if old:
        if any(value != payload for value in old):
            raise ValueError('immutable execution binding conflict')
        return
    store.record_evidence(run_id, Evidence(f'{run_id}:{kind}', todo_id, kind,
        'ProductionWorkspaceCoordinator', EvidenceStatus.PASSED, payload))


def persisted_responsibility(store, run_id):
    values = evidence_payloads(store, run_id, 'EXECUTION_RESPONSIBILITY_V1')
    if not values:
        return None
    if any(value != values[0] for value in values):
        raise ValueError('conflicting execution responsibility')
    return ExecutionResponsibilityV1(**values[0])


def root_snapshot(store, run_id):
    values = evidence_payloads(store, run_id, 'ACCEPTANCE_OBLIGATIONS_V1')
    if not values:
        return None
    if any(value != values[0] for value in values):
        raise ValueError('candidate cannot rewrite frozen acceptance')
    value = values[0]
    frozen = AcceptanceObligationsV1.from_dict(value['obligations'])
    if frozen.run_id != run_id or value['fingerprint'] != fingerprint(frozen.to_dict()):
        raise ValueError('frozen acceptance integrity mismatch')
    return frozen


def find_root_snapshot(store, todo_id):
    # Attempts are chronological; lexical ordering would place attempt-10 before 2.
    runs = sorted((run for run in store.list_runs() if run.active_todo_id == todo_id),
                  key=lambda run: int(run.run_id.rsplit('-attempt-', 1)[-1])
                  if '-attempt-' in run.run_id else 0)
    for run in runs:
        frozen = root_snapshot(store, run.run_id)
        if frozen is not None:
            if frozen.todo_id != todo_id:
                raise ValueError('root acceptance Todo mismatch')
            return frozen
    return None


def bind_acceptance_lineage(store, run_id, contract, responsibility, root):
    if root.todo_id != contract.todo_id:
        raise ValueError('root acceptance Todo mismatch')
    root_contract = next((json.loads(e.content) for e in root.evidence
                          if e.evidence_id == 'original_constraints'), None)
    if root_contract is None or root_contract['constraints'] != contract.to_dict()['constraints']:
        raise ValueError('replan cannot replace structured root constraints')
    if root_contract['required_evidence'] != contract.to_dict()['required_evidence']:
        raise ValueError('replan cannot replace root required evidence')
    contradictions = []
    if contract.constraints.get('REQUIRES_REGRESSION_CHANGE') is True and (
        'tests are immutable' in contract.objective.lower()
        or 'tests must not be modified' in contract.objective.lower()
    ):
        contradictions.append('REPLAN_GUIDANCE_CONFLICTS_WITH_REQUIRES_REGRESSION_CHANGE')
    record_once(store, run_id, contract.todo_id, 'EXECUTION_ACCEPTANCE_BINDING_V1', {
        'schema_version': 1, 'run_id': run_id, 'todo_id': contract.todo_id,
        'responsibility': responsibility.to_dict(),
        'root_run_id': root.run_id, 'root_acceptance_fingerprint': fingerprint(root.to_dict()),
        'execution_guidance_provenance': 'MODEL_GENERATED',
        'structured_constraints_fingerprint': fingerprint(contract.to_dict()['constraints']),
        'contradictions': contradictions, 'boundary': 'BEFORE_WORKER_EXECUTION',
    })


def load_acceptance_lineage(store, run_id, contract):
    values = evidence_payloads(store, run_id, 'EXECUTION_ACCEPTANCE_BINDING_V1')
    if not values:
        return None
    binding = values[0]
    responsibility = persisted_responsibility(store, run_id)
    if (any(value != binding for value in values) or type(binding['schema_version']) is not int or binding['schema_version'] != 1
        or binding['run_id'] != run_id or binding['todo_id'] != contract.todo_id
        or responsibility is None or binding['responsibility'] != responsibility.to_dict()
        or responsibility.todo_id != contract.todo_id
        or responsibility.work_contract_fingerprint != fingerprint(contract.to_dict())
        or binding['structured_constraints_fingerprint'] != fingerprint(contract.to_dict()['constraints'])
        or binding['execution_guidance_provenance'] != 'MODEL_GENERATED'):
        raise ValueError('acceptance lineage responsibility mismatch')
    root = root_snapshot(store, binding['root_run_id'])
    if (root is None or root.todo_id != contract.todo_id
        or fingerprint(root.to_dict()) != binding['root_acceptance_fingerprint']):
        raise ValueError('acceptance lineage root integrity mismatch')
    return root
