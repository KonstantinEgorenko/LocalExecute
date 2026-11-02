"""Opt-in, public-only task decomposition diagnostic. Never a production role."""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import time

from .agent_loop.protocol import decode_strict_json
from .config import ConfigLoader
from .judge import JudgePolicy
from .judge.protocol import parse_public_interpretations
from .model_performance_benchmark import NativeTransport, SequentialBenchmark, IsolationError, profiles_from_runtime
from harness.executor.profiles import ExecutionProfileRegistry
from .protocol_transport import ollama_compatible_schema
from .public_interpreter_capability import CapabilityCase, load_suite, evaluate_response
from .verification import VerificationRunner, VerificationPolicy
from .verification.acceptance import (AcceptanceObligationsV1, canonical, fingerprint,
    independent_obligation_refs, public_witness_binding, public_interpretation_request,
    public_witness_repair_request)
from .verification.witness_domain import validate_witness_inputs

MODEL_ID = 'qwen27_heavy_quality'
MODEL_TAG = 'qwen36-27b-heavy-quality:latest'
ENDPOINT = 'http://127.0.0.1:11435'
AUTHORITY = 'PUBLIC_AUTHORITY_RESOLUTION_V1'
SYNTHESIS = 'PUBLIC_WITNESS_SYNTHESIS_V1'
REPAIR = 'COMPACT_WITNESS_REPAIR_V1'
STAGES = (AUTHORITY, SYNTHESIS, REPAIR)
REVISION = 'PUBLIC_INTERPRETER_DECOMPOSITION_V2'
# Evaluation-only dispositions; never included in a model-facing projection.
EXPECTED_AUTHORITY = ('SUPPORTED', 'SUPPORTED', 'SUPPORTED', 'CONFLICTING', 'AMBIGUOUS')


from .judge.public_interpreter_contract import (
    PublicSemanticGroupV1, semantic_groups, authority_request, parse_authority,
    compact_envelope, witness_request as _shared_witness_request)


def witness_request(case, group, rejected=None):
    return _shared_witness_request(case.frozen, group, rejected)


def rejected_witness(case, group, runner, checked=None):
    if checked is None:
        rows = parse_public_interpretations(case.rejected_response, case.frozen)
        checked = replace(case.frozen, public_interpretations=validate_witness_inputs(
            case.frozen, rows, runner, decode=decode_strict_json))
    # Reuse E6.3 packet validation/localization, then remove duplicated envelope.
    request = public_witness_repair_request(case.frozen, checked,
        proposal_fingerprint=fingerprint([asdict(r) for r in checked.public_interpretations]), attempt_index=1)
    packet = decode_strict_json(request[1].split('\n', 1)[1])
    failed = [r for r in packet['failed_obligations'] if r['obligation']['obligation_id'] in group.obligation_ids]
    if len(failed) != len(group.obligation_ids) or any(r['validation']['status'] != 'INVALID' for r in failed):
        raise ValueError('fixed repair fixture must be definitively INVALID')
    witnesses = []
    for r in failed:
        w = r['previous_proposal']['witness']
        witnesses.append({k: w[k] for k in ('module_path', 'callable_name', 'expected_expression')} |
            {'arguments': decode_strict_json(w['arguments_json'])})
    if len({canonical(w) for w in witnesses}) != 1:
        raise ValueError('fixed repair fixture has unequal group witnesses')
    return {'witness': witnesses[0], 'validation': failed[0]['validation']}


@dataclass(frozen=True)
class Arm:
    case: CapabilityCase
    group: PublicSemanticGroupV1
    capability: str
    expected_status: str
    request: tuple

    def identity(self):
        return {'case_id': self.case.case_id, 'capability': self.capability,
            'group': asdict(self.group), 'expected_status': self.expected_status,
            'snapshot_fingerprint': fingerprint(self.case.frozen.to_dict()),
            'evaluation_fingerprint': fingerprint((self.case.disposition, self.case.oracle_expression)),
            'request_fingerprint': fingerprint(self.request)}


def prepare_arms(cases, common, runner):
    if len(cases) != 5:
        raise ValueError('five E7 cases required')
    groups = [semantic_groups(c.frozen) for c in (*cases, common[0])]
    if any(len(g) != 1 for g in groups):
        raise ValueError('canonical fixtures require one structural callable group each')
    arms = [Arm(c, g[0], AUTHORITY, status, authority_request(c.frozen, g))
        for c, g, status in zip(cases, groups, EXPECTED_AUTHORITY)]
    supported = (*cases[:3], common[0])
    supported_groups = (*groups[:3], groups[-1])
    for capability in (SYNTHESIS, REPAIR):
        for c, g in zip(supported, supported_groups):
            rejected = rejected_witness(c, g[0], runner,
                checked=common[1] if c is common[0] else None) if capability == REPAIR else None
            arms.append(Arm(c, g[0], capability, 'SUPPORTED', witness_request(c, g[0], rejected)))
    return tuple(arms)


def evaluate_arm(arm, raw, runner):
    if arm.capability == AUTHORITY:
        try:
            parsed = parse_authority(raw, (arm.group,))
            status = parsed['groups'][arm.group.group_id]['status']
            return {'parser_valid': True, 'public_refs_valid': True, 'observed_status': status,
                'semantic_disposition_correct': status == arm.expected_status}
        except (ValueError, TypeError) as exc:
            return {'parser_valid': False, 'semantic_disposition_correct': False,
                'failure_kind': 'AUTHORITY_PROTOCOL', 'error': str(exc)[:2048]}
    try:
        envelope = compact_envelope(raw, arm.group, arm.case.frozen)
        result = evaluate_response(arm.case, envelope, runner)
    except (ValueError, TypeError) as exc:
        return {'parser_valid': False, 'semantic_disposition_correct': False,
            'failure_kind': 'COMPACT_PROTOCOL', 'error': str(exc)[:2048]}
    if not result['semantic_disposition_correct']:
        result['failure_kind'] = ('BINDING' if result['binding_failure'] else
            'REFERENCE_EXPRESSION' if not result['parser_valid'] else
            'DOMAIN' if not result['domain_valid'] else 'SEMANTIC_RELATION')
    return result


def byte_sizes(request, payload):
    system, user, schema = request
    return {'prompt_bytes': len((system + user).encode('utf-8')),
        'schema_bytes': len(json.dumps(ollama_compatible_schema(schema)).encode('utf-8')),
        # Match OllamaNativeClient._request's actual json.dumps wire encoding.
        'request_bytes': len(json.dumps(payload).encode('utf-8'))}


class DecompositionScreen:
    def __init__(self, transport, profile, arms, runner, *, temperature=0.0, policy=None,
                 expected_digest=None, save=None, resume=None, clock=time.perf_counter):
        if profile.profile_id != MODEL_ID or profile.model != MODEL_TAG or profile.endpoint.rstrip('/') != ENDPOINT:
            raise ValueError('exact Qwen27 production profile/endpoint required')
        self.transport, self.profile, self.arms, self.runner = transport, profile, arms, runner
        self.temperature, self.policy = temperature, policy or JudgePolicy()
        if (self.policy.timeout_seconds, self.policy.output_tokens, self.policy.max_response_bytes) != (180, 4096, 65536):
            raise ValueError('E8 requires existing production 180s/4096/65536 budget')
        self.save, self.clock, self.expected_digest = save or (lambda r: None), clock, expected_digest
        self.isolation = SequentialBenchmark(transport, (profile,))
        self.identity = fingerprint({'revision': REVISION, 'profile': asdict(profile), 'arms': [a.identity() for a in arms],
            'policy': asdict(self.policy), 'temperature': temperature, 'expected_digest': expected_digest})
        self.report = {'diagnostic': REVISION, 'shared_language_contract_aligned': True, 'status': 'IN_PROGRESS',
            'identity': self.identity, 'profile': asdict(profile), 'policy': asdict(self.policy),
            'temperature': temperature, 'production_changed': False, 'candidate_calls': 0, 'hidden_calls': 0,
            'mission_calls': 0, 'retry_calls': 0, 'planned_calls': len(arms), 'fatal_error': None,
            'samples': [{**a.identity(), 'status': 'NOT_RUN', 'calls': 0} for a in arms],
            'unload_diagnostics': self.isolation.report['unload_diagnostics']}
        if resume is not None:
            if (resume['diagnostic'] != REVISION or resume['identity'] != self.identity
                or resume['status'] != 'STOPPED_RUNTIME_BOUNDARY'):
                raise ValueError('resume identity/status mismatch')
            self.report = copy.deepcopy(resume)
            if len(self.report['samples']) != len(arms):
                raise ValueError('resume arm count mismatch')
            pending_seen = False
            for sample, arm in zip(self.report['samples'], arms):
                if any(sample.get(k) != v for k, v in arm.identity().items()):
                    raise ValueError('resume arm identity mismatch')
                if sample['status'] == 'NOT_RUN':
                    pending_seen = True
                elif sample['status'] not in ('PASS', 'FAIL', 'TIMEOUT', 'TRANSPORT_FAILURE') or pending_seen or 'outcome' not in sample:
                    raise ValueError('resume requires retained terminal prefix; never repeat indeterminate calls')
            self.report.setdefault('interruptions', []).append(self.report['fatal_error'])
            self.report['fatal_error'] = None
            self.report['status'] = 'IN_PROGRESS'
            self.isolation.report['unload_diagnostics'] = self.report['unload_diagnostics']

    def persist(self):
        self.report['physical_calls'] = sum(s['calls'] for s in self.report['samples'])
        self.save(self.report)

    def payload(self, arm):
        system, user, schema = arm.request
        if len((system + user).encode('utf-8')) > self.policy.max_prompt_bytes:
            raise ValueError('diagnostic prompt exceeds production bound')
        return {'model': self.profile.model, 'messages': [{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], 'format': ollama_compatible_schema(schema),
            'think': False, 'stream': False, 'keep_alive': -1, 'options': {'num_ctx': self.profile.num_ctx,
                'num_gpu': self.profile.num_gpu, 'temperature': self.temperature, 'num_predict': self.policy.output_tokens}}

    def sample(self, arm, sample, digest):
        payload = self.payload(arm)
        sample.update(status='IN_PROGRESS', calls=1, raw_response=None, response_bytes=None,
            logical_role=arm.capability, logical_model_id=MODEL_ID, concrete_model_tag=MODEL_TAG,
            digest=digest, request_payload=payload, byte_sizes=byte_sizes(arm.request, payload))
        # Persist before physical call; a crash must never authorize resampling.
        self.persist()
        started = self.clock()
        try:
            data = self.transport.request(self.profile.endpoint, '/api/chat', payload)
        except Exception as exc:
            sample.update(status='TIMEOUT' if 'timed out' in str(exc).lower() or isinstance(exc, TimeoutError)
                else 'TRANSPORT_FAILURE', generation_wall_seconds=self.clock()-started,
                outcome={'parser_valid': False, 'semantic_disposition_correct': False,
                    'failure_kind': 'TIMEOUT' if 'timed out' in str(exc).lower() or isinstance(exc, TimeoutError) else 'TRANSPORT',
                    'exception_type': type(exc).__name__, 'error': str(exc)[:2048]})
            self.persist()
            raise IsolationError('generation transport failure; resume only after verified empty residency') from exc
        sample['generation_wall_seconds'] = self.clock()-started
        sample['response_model'] = data.get('model')
        if data.get('model') != MODEL_TAG:
            raise IsolationError('exact returned model identity mismatch')
        sample['observed_placement'] = self.isolation.verify(self.profile, digest)
        raw = data.get('message', {}).get('content')
        if type(raw) is str:
            sample['response_bytes'] = len(raw.encode('utf-8'))
            sample['raw_response'] = raw.encode('utf-8')[:self.policy.max_response_bytes].decode('utf-8', errors='ignore')
        sample['done'], sample['done_reason'] = data.get('done'), data.get('done_reason')
        if (type(raw) is not str or sample['response_bytes'] > self.policy.max_response_bytes
            or data.get('done') is not True or data.get('done_reason') == 'length'):
            sample['outcome'] = {'parser_valid': False, 'semantic_disposition_correct': False,
                'failure_kind': 'TRUNCATED_OR_MALFORMED_RESPONSE', 'error': 'missing, oversized or unfinished response'}
        else:
            sample['outcome'] = evaluate_arm(arm, raw, self.runner)
        sample['status'] = 'PASS' if sample['outcome']['semantic_disposition_correct'] else 'FAIL'
        self.persist()

    def run(self):
        # Validate complete prepared plan and request bounds before runtime mutation.
        if len(self.arms) != 13 or [sum(a.capability == stage for a in self.arms) for stage in STAGES] != [5, 4, 4]:
            raise ValueError('canonical E8 diagnostic plan requires 5+4+4 arms')
        for arm in self.arms:
            self.payload(arm)
        try:
            available = self.transport.request(self.profile.endpoint, '/api/tags')
            if type(available) is not dict or type(available.get('models')) is not list:
                raise IsolationError('malformed exact availability')
            matches = [m for m in available['models'] if m.get('name') == MODEL_TAG and m.get('model', MODEL_TAG) == MODEL_TAG]
            self.report['availability'] = matches
            if len(matches) != 1 or not matches[0].get('digest'):
                self.report['status'] = 'NOT_RUN';self.report['reason'] = 'exact Qwen27 model/tag/digest unavailable'
                return self.report
            digest = matches[0]['digest']
            if (self.expected_digest is not None and digest != self.expected_digest) or self.report.get('digest', digest) != digest:
                raise IsolationError('exact model digest changed')
            self.report['digest'] = digest
            residents = self.isolation.residents()
            if any(m.get('name', m.get('model')) != MODEL_TAG or m.get('digest') != digest for _, m in residents):
                raise IsolationError('foreign residency; do not unload another model')
            self.isolation.empty()
            for stage in STAGES:
                stage_rows = [(arm, sample) for arm, sample in zip(self.arms, self.report['samples']) if arm.capability == stage]
                for arm, sample in stage_rows:
                    if sample['status'] == 'NOT_RUN':
                        self.sample(arm, sample, digest)
            # V2 is a fresh fixed 13-arm measurement, not an adaptive mission.
            # Measure independent supported fixtures even if another arm fails.
            failed = [s for s in self.report['samples'] if s['status'] != 'PASS']
            semantic_failures = [s for s in failed if s['status'] not in ('TIMEOUT', 'TRANSPORT_FAILURE')]
            if not failed:
                self.report['status'] = 'DECOMPOSED_LOCAL_CAPABILITY_QUALIFIED'
            elif not semantic_failures:
                self.report['status'] = 'PUBLIC_INTERPRETER_RUNTIME_BUDGET_GAP'
            else:
                first = semantic_failures[0]
                self.report['first_failing_stage'] = first['capability']
                self.report['status'] = ('PUBLIC_AUTHORITY_RESOLUTION_MODEL_GAP'
                    if first['capability'] == AUTHORITY else 'PUBLIC_WITNESS_SYNTHESIS_MODEL_GAP')
            self.isolation.empty()
            self.report['final_residents'] = self.isolation.residents()
        except Exception as exc:
            self.report['status'] = 'STOPPED_RUNTIME_BOUNDARY'
            self.report['fatal_error'] = {'type': type(exc).__name__, 'message': str(exc)[:2048]}
            raise
        finally:
            self.persist()
        return self.report


def render(report):
    lines = ['# PHASE25E.8.1 - PublicInterpreter decomposition V2 diagnostic',
        f"Status: **{report['status']}**. Production binding unchanged; shared grammar contract aligned. No Mission, candidate or hidden execution.",
        f"Exact model: `{MODEL_TAG}`; digest `{report.get('digest')}`; placement `{report['profile']}`.",
        'Fresh fixed screen: authority5/5, initial4/4, fixed repair4/4. Measure all 13 independent arms; no retries. Transport isolation failure stops until verified empty residency.',
        '| Case | Capability | Result | Observed disposition / failure | Prompt bytes | Schema bytes | Request bytes | Response bytes | Wall seconds |',
        '|---|---|---|---|---:|---:|---:|---:|---:|']
    for s in report['samples']:
        sizes = s.get('byte_sizes', {})
        outcome = s.get('outcome', {})
        detail = outcome.get('observed_status', outcome.get('failure_kind', ''))
        lines.append(f"| {s['case_id']} | {s['capability']} | {s['status']} | {detail} | {sizes.get('prompt_bytes')} | {sizes.get('schema_bytes')} | {sizes.get('request_bytes')} | {s.get('response_bytes')} | {s.get('generation_wall_seconds')} |")
    lines += ['', f"Physical generation calls: {report.get('physical_calls', 0)}/13 planned maximum; retry calls0. Raw response/request references: companion JSON `samples`, keyed by case/capability.",
        f"Verified final residents: {report.get('final_residents', 'NOT_VERIFIED')}. Unload evidence: companion JSON `unload_diagnostics`.",
        'This is a small capability diagnostic, not statistical reliability proof. Historical E8/E7 evidence is immutable; E6 remains1/3. No SIMPLE-Q1 or DIRECT. Production integration/call-budget design is deferred.']
    return '\n\n'.join(lines[:4]) + '\n\n' + '\n'.join(lines[4:]) + '\n'


def load_inputs(suite, fixture, runner):
    cases = load_suite(suite)
    common = decode_strict_json(Path(fixture).read_text(encoding='utf-8'))
    frozen, checked = (AcceptanceObligationsV1.from_dict(common[k]) for k in ('frozen', 'checked'))
    case = CapabilityCase('e63_rejected_proposal', 'VALID_WITNESS', frozen, '', common['oracle_expression'], True)
    return prepare_arms(cases, (case, checked), runner)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true');p.add_argument('--resume', action='store_true')
    p.add_argument('--suite', type=Path, required=True);p.add_argument('--common-repair-fixture', type=Path, required=True)
    p.add_argument('--e7-results', type=Path, required=True);p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not args.live:p.error('--live required')
    if args.output.exists() and not args.resume:raise FileExistsError(args.output)
    root = Path.cwd()
    policy = JudgePolicy.from_policies(ConfigLoader(root).load_policies())
    profile, = profiles_from_runtime(root, 'production', model_id=MODEL_ID)
    registry = ExecutionProfileRegistry.from_project_root(root)
    execution_profiles = [registry.get(k) for k in registry.profile_ids if registry.get(k).model_id == MODEL_ID]
    if len(execution_profiles) != 1:raise ValueError('production temperature profile unresolved')
    runner = VerificationRunner(manager=None, policy=VerificationPolicy(trusted_python_executable=str(root/'.venv/Scripts/python.exe')))
    arms = load_inputs(args.suite, args.common_repair_fixture, runner)
    e7 = decode_strict_json(args.e7_results.read_text(encoding='utf-8'))
    previous, = [m for m in e7['models'] if m['model_id'] == MODEL_ID]
    resume = decode_strict_json(args.output.read_text(encoding='utf-8')) if args.resume else None
    def save(report):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.pending.json')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(args.output)
        args.output.with_suffix('.md').write_text(render(report), encoding='utf-8')
    screen = DecompositionScreen(NativeTransport(timeout=policy.timeout_seconds), profile, arms, runner,
        temperature=execution_profiles[0].temperature, policy=policy, expected_digest=previous['digest'], save=save, resume=resume)
    report = screen.run()
    print(json.dumps({'status': report['status'], 'physical_calls': report['physical_calls']}))


if __name__ == '__main__':
    main()
