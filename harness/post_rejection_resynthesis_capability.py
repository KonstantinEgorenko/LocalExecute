"""Opt-in E8.2 diagnostic. Rejected semantics are audit data, never model input."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import uuid

from .agent_loop.protocol import decode_strict_json
from .config import ConfigLoader
from .judge import JudgePolicy
from .judge.protocol import parse_public_interpretations
from .model_performance_benchmark import NativeTransport, IsolationError, profiles_from_runtime
from harness.executor.profiles import ExecutionProfileRegistry
from .public_interpreter_capability import CapabilityCase, load_suite
from .public_interpreter_decomposition_capability import (
    Arm, DecompositionScreen, AUTHORITY, SYNTHESIS, REPAIR, REVISION as E81_REVISION,
    MODEL_ID, MODEL_TAG, semantic_groups, witness_request, rejected_witness)
from .verification import VerificationRunner, VerificationPolicy
from .verification.acceptance import AcceptanceObligationsV1, canonical, fingerprint
from .verification.witness_domain import PublicWitnessInputValidationV1, validate_witness_inputs
from .judge.public_interpreter_contract import (
    RECOVERY_KEYS, FRESH_INSTRUCTION, recovery_state, fresh_witness_request)

ROLE = 'POST_REJECTION_RESYNTHESIS_V1'
CASES = ('fraction_fee', 'percentage_markup', 'ordered_points', 'e63_rejected_proposal')


def resynthesis_request(case, group, validation, *, authority_status):
    if authority_status != 'SUPPORTED' or case.disposition != 'VALID_WITNESS':
        raise ValueError('resynthesis requires previously SUPPORTED authority')
    return fresh_witness_request(case.frozen, group, validation)


def packet_proof(arm):
    """Exact projection comparison, not a search for numeric substrings."""
    initial_system, initial_user, initial_schema = witness_request(arm.case, arm.group)
    packet = decode_strict_json(arm.request[1])
    expected = decode_strict_json(initial_user)
    control = decode_strict_json(arm.rejected_artifact_json)
    validations = [PublicWitnessInputValidationV1(**{**v, 'public_refs':tuple(v['public_refs'])})
        for v in control['verified_validations']]
    proposals = {p['obligation_id']:p for p in control['previous_proposal']}
    if (len(proposals) != len(control['previous_proposal']) or set(proposals) != set(arm.group.obligation_ids)
            or {v.obligation_id for v in validations} != set(proposals) or len(validations) != len(proposals)):
        raise ValueError('rejected artifact must cover exactly this group')
    for validation in validations:
        proposal = proposals[validation.obligation_id]
        if (not proposal['witness'] or proposal['unresolved']
                or validation.run_id != arm.case.frozen.run_id or validation.todo_id != arm.case.frozen.todo_id
                or validation.public_snapshot_fingerprint != fingerprint(arm.case.frozen.to_dict())
                or (validation.origin_path,validation.origin_symbol,validation.signature_fingerprint) !=
                    (arm.group.module_path,arm.group.callable_name,arm.group.signature_fingerprint)
                or validation.arguments_fingerprint != fingerprint(decode_strict_json(proposal['witness']['arguments_json']))
                or tuple(proposal['public_refs']) != validation.public_refs):
            raise ValueError('validation must bind the original rejected hypothesis and frozen group')
    states = [recovery_state(v) for v in validations]
    if not states or any(s != states[0] for s in states):
        raise ValueError('group requires one consistent verified validation class')
    expected['recovery_state'] = states[0]
    if (packet != expected or arm.request[0] != initial_system + FRESH_INSTRUCTION
            or arm.request[2] != initial_schema):
        raise ValueError('resynthesis packet differs from frozen authority plus typed state')
    return {'authority_projection_matches_initial':True,
        'added_top_level_fields':['recovery_state'], 'recovery_keys':list(RECOVERY_KEYS),
        'response_schema_matches_initial':True, 'rejected_semantics_exposed_to_model':False}


@dataclass(frozen=True)
class ResynthesisArm(Arm):
    attempt_id: str
    rejected_artifact_reference: str
    rejected_artifact_json: str
    support_observation_fingerprint: str
    support_observation_kind: str

    def identity(self):
        control = decode_strict_json(self.rejected_artifact_json)
        identity = {**super().identity(), 'attempt_id':self.attempt_id,
            'group_fingerprint':fingerprint(asdict(self.group)),
            'rejected_proposal_fingerprint':fingerprint(control['previous_proposal']),
            'validation_fingerprint':fingerprint(control['verified_validations']),
            'rejected_artifact_reference':self.rejected_artifact_reference,
            'support_observation_fingerprint':self.support_observation_fingerprint,
            'support_observation_kind':self.support_observation_kind}
        identity['model_request_fingerprint'] = identity.pop('request_fingerprint')
        identity['request_fingerprint'] = fingerprint(identity)
        return identity


def prepare_arms(suite, fixture, historical, runner, *, run_id):
    """Recheck controlled rejections with the unchanged original-input validator."""
    if not run_id or historical.get('diagnostic') != E81_REVISION:
        raise ValueError('fresh identity and immutable E8.1 V2 evidence required')
    cases = load_suite(suite)
    common = decode_strict_json(Path(fixture).read_text(encoding='utf-8'))
    frozen = AcceptanceObligationsV1.from_dict(common['frozen'])
    saved = AcceptanceObligationsV1.from_dict(common['checked'])
    if replace(saved, public_interpretations=()) != frozen:
        raise ValueError('controlled rejected snapshot mismatch')
    common_case = CapabilityCase(CASES[-1], 'VALID_WITNESS', frozen, '', common['oracle_expression'], True)
    arms = []
    for case in (*cases[:3], common_case):
        group, = semantic_groups(case.frozen)
        observations = [s for s in historical['samples'] if s['case_id'] == case.case_id
            and s['capability'] == AUTHORITY]
        # E8.1's five authority calls exclude E6.3. Its existing successful
        # SUPPORTED initial arm is the eligibility evidence, not a new resolver result.
        if not observations and case is common_case:
            observations = [s for s in historical['samples'] if s['case_id'] == case.case_id
                and s['capability'] == SYNTHESIS]
        if len(observations) != 1:
            raise ValueError('matching prior SUPPORTED observation required')
        authority = observations[0]
        outcome = authority['outcome']
        supported = (outcome.get('public_refs_valid') and outcome.get('observed_status') == 'SUPPORTED'
            if authority['capability'] == AUTHORITY else
            all(outcome.get(k) is True for k in ('grounding_valid','obligations_complete',
                'witness_syntax_valid','expression_valid','domain_valid',
                'semantic_disposition_correct','public_relation_match')))
        if (authority['status'] != 'PASS' or authority['expected_status'] != 'SUPPORTED'
                or not outcome['parser_valid'] or not supported
                or authority['group'] != json.loads(canonical(asdict(group)))
                or authority['snapshot_fingerprint'] != fingerprint(case.frozen.to_dict())):
            raise ValueError('matching prior SUPPORTED authority required')
        rows = saved.public_interpretations if case is common_case else parse_public_interpretations(
            case.rejected_response, case.frozen)
        verified = validate_witness_inputs(case.frozen, rows, runner, decode=decode_strict_json)
        checked = replace(case.frozen, public_interpretations=verified)
        # Reuse existing group-local rejection checks; never send this packet.
        rejected_witness(case, group, runner, checked=checked)
        validations = [i.input_validation for i in verified]
        states = [recovery_state(v) for v in validations]
        if not states or any(s != states[0] for s in states):
            raise ValueError('inconsistent group validation')
        artifact = canonical({'previous_proposal':[asdict(replace(i,input_validation=None)) for i in rows],
            'verified_validations':[asdict(v) for v in validations]})
        reference = (str(fixture)+'#/checked') if case is common_case else str(suite)+'#/cases/'+case.case_id
        arms.append(ResynthesisArm(case, group, ROLE, 'SUPPORTED',
            resynthesis_request(case, group, validations[0], authority_status=authority['expected_status']),
            run_id+':'+case.case_id+':fixed', reference, artifact, fingerprint(authority),authority['capability']))
    if tuple(a.case.case_id for a in arms) != CASES:
        raise ValueError('canonical four supported fixtures required')
    # Independent identities and identical semantic packets, declared before observations.
    arms.extend(replace(arms[3], attempt_id=run_id+':'+CASES[-1]+':qualification:'+str(n)) for n in (2,3))
    for arm in arms:
        packet_proof(arm)
    return tuple(arms)


class ResynthesisScreen(DecompositionScreen):
    """Reuse existing physical transport/schema/evaluation/isolation primitives."""
    def __init__(self, transport, profile, arms, runner, *, historical, run_id, **kwargs):
        if kwargs.get('resume') is not None:
            raise ValueError('E8.2 does not retry or resume interrupted generation')
        if (len(arms) != 6 or tuple(a.case.case_id for a in arms[:4]) != CASES
                or any(a.capability != ROLE for a in arms)
                or len({a.attempt_id for a in arms}) != 6
                or any(a.case.case_id != CASES[-1] for a in arms[4:])
                or any(a.request != arms[3].request or a.rejected_artifact_json != arms[3].rejected_artifact_json
                    for a in arms[4:])):
            raise ValueError('four fixed arms plus two predeclared identical E6.3 repetitions required')
        super().__init__(transport, profile, arms, runner, expected_digest=historical['digest'], **kwargs)
        if (asdict(profile) != historical['profile'] or self.temperature != historical['temperature']
                or asdict(self.policy) != historical['policy']):
            raise ValueError('exact E8.1 runtime profile/temperature/budgets required')
        self.identity = fingerprint({'revision':ROLE,'parent_identity':self.identity,'run_id':run_id})
        self.report.update(diagnostic=ROLE, identity=self.identity, run_id=run_id,
            historical_e81_fingerprint=fingerprint(historical), production_integration='DEFERRED',
            qualification_rule='Execute two predeclared E6.3 repetitions only if fixed four arms all PASS',
            fixed_screen_status='NOT_RUN', final_residents='NOT_VERIFIED')
        for arm, sample in zip(arms, self.report['samples']):
            sample.update(packet_proof=packet_proof(arm), rejected_semantics_exposed_to_model=False,
                control_plane=decode_strict_json(arm.rejected_artifact_json))

    def sample(self, arm, sample, digest):
        packet_proof(arm)
        try:
            super().sample(arm, sample, digest)
        finally:
            outcome = sample.get('outcome', {})
            sample.update(parser_valid=outcome.get('parser_valid',False),
                binding_valid=bool(outcome.get('parser_valid') and not outcome.get('binding_failure')),
                expression_valid=outcome.get('expression_valid',False),
                domain_valid=outcome.get('domain_valid',False),
                semantic_relation_result=outcome.get('public_relation_match'))
            self.persist()

    def run(self):
        for arm in self.arms:
            packet_proof(arm); self.payload(arm)
        self.persist()  # All six arms, including conditional repetitions, precede live work.
        try:
            available = self.transport.request(self.profile.endpoint, '/api/tags')
            if type(available) is not dict or type(available.get('models')) is not list:
                raise IsolationError('malformed exact availability')
            matches = [m for m in available['models'] if m.get('name') == MODEL_TAG
                and m.get('model', MODEL_TAG) == MODEL_TAG]
            if len(matches) != 1 or matches[0].get('digest') != self.expected_digest:
                raise IsolationError('exact historical model/tag/digest unavailable')
            digest = self.report['digest'] = matches[0]['digest']
            if any(m.get('name',m.get('model')) != MODEL_TAG or m.get('digest') != digest
                    for _,m in self.isolation.residents()):
                raise IsolationError('foreign residency; do not unload another model')
            self.isolation.empty()
            for arm, sample in zip(self.arms[:4], self.report['samples'][:4]):
                self.sample(arm, sample, digest)
            failed = [s for s in self.report['samples'][:4] if s['status'] != 'PASS']
            self.report['fixed_screen_status'] = 'FAIL' if failed else 'PASS'
            self.persist()
            if failed:
                self.report['status'] = ('POST_REJECTION_RESYNTHESIS_REGRESSION'
                    if any(s['case_id'] != CASES[-1] for s in failed) else 'POST_REJECTION_RECOVERY_MODEL_GAP')
                for s in self.report['samples'][4:]:
                    s['not_run_reason'] = 'FIXED_SCREEN_NOT_QUALIFIED'
            else:
                for arm, sample in zip(self.arms[4:], self.report['samples'][4:]):
                    self.sample(arm, sample, digest)
                self.report['status'] = ('POST_REJECTION_RESYNTHESIS_QUALIFIED'
                    if all(s['status'] == 'PASS' for s in self.report['samples'])
                    else 'POST_REJECTION_RESYNTHESIS_UNSTABLE')
            self.isolation.empty()
            self.report['final_residents'] = self.isolation.residents()
        except Exception as exc:
            self.report.update(status='POST_REJECTION_RESYNTHESIS_RUNTIME_GAP',
                fatal_error={'type':type(exc).__name__,'message':str(exc)[:2048]})
        finally:
            self.persist()
        return self.report


def render(report, historical):
    lines = ['# PHASE25E.8.2 - Post-rejection fresh resynthesis', '',
        '**'+report['status']+'**. Diagnostic only; production integration deferred.', '',
        '| Case | Historical initial | Historical anchored repair | Fresh resynthesis |',
        '|---|---|---|---|']
    for case_id in CASES:
        old = [next(s for s in historical['samples'] if s['case_id']==case_id and s['capability']==cap)
            for cap in (SYNTHESIS, REPAIR)]
        new = [s for s in report['samples'] if s['case_id']==case_id]
        label = lambda s:s['status']+(' '+s.get('outcome',{}).get('failure_kind','')).rstrip()
        lines.append('| '+case_id+' | '+label(old[0])+' | '+label(old[1])+' | '+', '.join(label(s) for s in new)+' |')
    lines += ['', 'Calls: '+str(report.get('physical_calls',0))+'/6 maximum; retries0. Fixed screen: '+report['fixed_screen_status']+'.',
        'Raw responses, exact model/profile/digest, request/validation/rejected-artifact identities and structural packet proofs: companion JSON samples.',
        'Control-plane rejected artifacts are retained separately; model packets equal initial frozen authority plus typed recovery_state only.',
        'Prior anchored repair remains FAIL; it is never relabeled by this diagnostic.',
        'E6.3 eligibility uses its verified SUPPORTED initial PASS: E8.1 had no separate E6.3 authority call. Other fixtures use their saved authority PASS.',
        'If fresh E6.3 succeeds, evidence is consistent with repair-context anchoring risk; it is not causal proof or statistical reliability proof.',
        'If fresh E6.3 fails, post-rejection recovery remains an independent capability gap.',
        'Residency: '+str(report['final_residents'])+'. Unload acknowledgement/polls: companion JSON unload_diagnostics.']
    return '\n'.join(lines)+'\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--suite',type=Path,required=True)
    parser.add_argument('--common-repair-fixture',type=Path,required=True)
    parser.add_argument('--e81-results',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if not args.live:parser.error('--live required')
    if args.output.exists():raise FileExistsError(args.output)
    historical = decode_strict_json(args.e81_results.read_text(encoding='utf-8'))
    root = Path.cwd();run_id = 'e82:'+uuid.uuid4().hex
    runner = VerificationRunner(manager=None,policy=VerificationPolicy(
        trusted_python_executable=str(root/'.venv/Scripts/python.exe')))
    arms = prepare_arms(args.suite,args.common_repair_fixture,historical,runner,run_id=run_id)
    profile, = profiles_from_runtime(root,'production',model_id=MODEL_ID)
    registry = ExecutionProfileRegistry.from_project_root(root)
    worker, = [registry.get(k) for k in registry.profile_ids if registry.get(k).model_id == MODEL_ID]
    policy = JudgePolicy.from_policies(ConfigLoader(root).load_policies())
    def save(report):
        args.output.parent.mkdir(parents=True,exist_ok=True)
        pending = args.output.with_suffix('.pending.json')
        pending.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        pending.replace(args.output)
        args.output.with_suffix('.md').write_text(render(report,historical),encoding='utf-8')
    screen = ResynthesisScreen(NativeTransport(timeout=policy.timeout_seconds),profile,arms,runner,
        historical=historical,run_id=run_id,temperature=worker.temperature,policy=policy,save=save)
    screen.report['historical_e81_reference'] = str(args.e81_results)
    report = screen.run()
    print(json.dumps({'status':report['status'],'physical_calls':report['physical_calls']}))


if __name__ == '__main__':main()
