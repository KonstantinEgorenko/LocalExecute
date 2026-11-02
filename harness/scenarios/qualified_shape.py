"""Proof-bound bounded capability, separate from the model's task intent."""
from dataclasses import dataclass, asdict
from .scope_evidence import canonical, digest, PlanningContractError, SCOPE_EVIDENCE, SCOPE_REQUIREMENTS

SHAPE = 'QUALIFIED_EXECUTION_SHAPE_V1'
RULE = 'VERIFIED_TWO_SOURCE_ONE_REGRESSION_V1'
CAPABILITY = 'STANDARD_NATIVE_MULTIFILE_SPLIT_V1'


@dataclass(frozen=True, slots=True)
class QualifiedExecutionShapeV1:
    raw_selection_json: str
    manifest_json: str
    capability: str = CAPABILITY
    rule_id: str = RULE
    rationale: str = 'Verified required two-module mutation and one regression responsibility; bounded native eligibility.'

    def to_dict(self):
        body = asdict(self)
        return {**body, 'fingerprint': digest(canonical(body))}

    @classmethod
    def from_dict(cls, value):
        from .scope_evidence import plain
        body = plain(value); fp = body.pop('fingerprint', None)
        if fp != digest(canonical(body)):
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='shape fingerprint')
        return cls(**body)

    def facts(self):
        import json
        raw, manifest = json.loads(self.raw_selection_json), json.loads(self.manifest_json)
        check = dict(manifest); fp = check.pop('fingerprint', None)
        if (self.capability != CAPABILITY or self.rule_id != RULE
                or fp != digest(canonical(check))
                or raw['scenario'] not in ('BUGFIX_WITH_REGRESSION', 'MULTI_FILE_CHANGE')
                or raw['scope'] != 'MULTI_FILE' or not raw['requires_regression_change']
                or raw['requires_research'] or raw['risk'] != 'LOW' or raw['ambiguity'] != 'LOW'
                or len(manifest['required_source_paths']) != 2 or len(manifest['regression_paths']) != 1
                or len({manifest['canonical_paths'][p] for p in manifest['required_source_paths']}) != 2
                or not 0 < manifest['source_bytes'] <= 32768
                or set(manifest['allowed_paths']) != set(manifest['required_source_paths'] + manifest['regression_paths'])):
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_UNSUPPORTED', reason='intent/topology/regression/eligibility mismatch')
        return raw, manifest

    def validate_assessment(self, assessment):
        raw, manifest = self.facts()
        return (assessment.scenario == raw['scenario'] and assessment.scope == raw['scope']
                and assessment.ambiguity == assessment.risk == 'LOW'
                and assessment.target_confidence == assessment.verification_confidence == 'HIGH'
                and not assessment.research_required and not assessment.previous_failure
                and assessment.regression_required and assessment.multifile_native is True
                and set(assessment.source_targets) == set(manifest['required_source_paths'])
                and len(assessment.source_targets) == 2
                and tuple(assessment.test_targets) == tuple(manifest['regression_paths']))

    def validate_contract(self, contract, required_gates):
        raw, manifest = self.facts()
        c = contract.constraints
        if (contract.goal_id != manifest['goal_id'] or contract.objective != raw['objective']
                or canonical(c.get(SCOPE_EVIDENCE)) != self.manifest_json
                or digest(canonical(c.get(SCOPE_REQUIREMENTS))) != manifest['declaration_sha256']
                or c.get('SCENARIO_TYPE') != raw['scenario'] or c.get('SCENARIO_SCOPE') != raw['scope']
                or c.get('SCENARIO_RISK') != raw['risk'] or c.get('SCENARIO_AMBIGUITY') != raw['ambiguity']
                or c.get('REQUIRES_REGRESSION_CHANGE') is not True
                or type(c.get('NATIVE_MULTIFILE_SOURCE_LIMIT')) is not int or c['NATIVE_MULTIFILE_SOURCE_LIMIT'] != 2
                or c.get('research_required') is not False or c.get('semantic_replan_required')
                or c.get('recovery_context') or contract.quota.max_worker_calls < 4
                or not {'candidate_diff','scope_guard','python_syntax','pytest','regression_change'} <= set(required_gates)):
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='compiled responsibility/verification/budget mismatch')

    def validate_item(self, item):
        self.validate_contract(item.work_contract, item.todo.required_gates)
        from harness.router import Scope, TaskType
        raw, _ = self.facts()
        expected = TaskType.BUGFIX if raw['scenario'] == 'BUGFIX_WITH_REGRESSION' else TaskType.CODE
        if (item.todo.objective != item.work_contract.objective or item.todo.dependencies
                or item.routing_task_spec.scope is not Scope.MULTI_FILE
                or item.routing_task_spec.task_type is not expected):
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='compiled Todo responsibility mismatch')


def qualify(selection, evidence, request):
    from harness.execution_policy import TaskAssessmentV1, ExecutionPolicyV1
    if evidence is None:
        raise PlanningContractError('PLANNER_SCOPE_EVIDENCE_INVALID', reason='native multifile requires authoritative evidence')
    c = request.constraints
    if type(c.get('NATIVE_MULTIFILE_SOURCE_LIMIT')) is not int or c['NATIVE_MULTIFILE_SOURCE_LIMIT'] != 2:
        raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_UNSUPPORTED', reason='bounded source operation contract missing')
    for name in ('SOURCE_REPAIR_GENERATIONS', 'TEST_REPAIR_GENERATIONS'):
        if type(c.get(name, 0)) is not int or c.get(name, 0) not in (0, 1):
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_UNSUPPORTED', reason='repair budget exceeds bound')
    shape = QualifiedExecutionShapeV1(canonical(selection.to_dict()), evidence.document)
    raw, manifest = shape.facts()
    # Known local relationship is evidence, not a guess from API/file counts.
    required = set(manifest['required_source_paths'])
    if not any(edge[0] in required and edge[2] in required and edge[0] != edge[2]
               for edge in manifest['verified_dependency_edges']):
        raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_UNSUPPORTED', reason='verified local dependency missing')
    assessment = TaskAssessmentV1(raw['scenario'], 7, raw['scope'], raw['ambiguity'], raw['risk'],
        'HIGH', 'HIGH', raw['requires_research'], raw['requires_regression_change'],
        tuple(manifest['required_source_paths']), tuple(manifest['regression_paths']),
        multifile_native=True, qualified_shape=shape)
    if ExecutionPolicyV1.select(assessment).capability != CAPABILITY:
        raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_UNSUPPORTED', reason='assessment rejected')
    return shape
