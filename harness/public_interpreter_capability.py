"""Opt-in role screening. No Mission state, routing, candidate or hidden oracle."""
from __future__ import annotations
import argparse
import copy
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics
import time

from .agent_loop.protocol import decode_strict_json
from .control_plane import WorkContract, Quota
from .config import ConfigLoader
from .judge import JudgePolicy
from .judge.protocol import parse_public_interpretations
from .model_performance_benchmark import NativeTransport, SequentialBenchmark, profiles_from_runtime, IsolationError
from .protocol_transport import ollama_compatible_schema
from .verification import VerificationPolicy, VerificationRunner
from .verification.acceptance import (AcceptanceObligationsV1, build_acceptance_obligations,
    public_interpretation_request, public_witness_repair_request, fingerprint, canonical)
from .verification.public_reference import evaluate_public_reference
from .verification.witness_domain import validate_witness_inputs

MODELS={'ornith_lead':'hf.co/deepreinforce-ai/Ornith-1.0-35B-GGUF:Q4_K_M',
    'qwen27_heavy_quality':'qwen36-27b-heavy-quality:latest',
    'qwen35_heavy_fast':'qwen36-35b-a3b-heavy-fast:latest'}
CASES=('fraction_fee','percentage_markup','ordered_points','conflicting_timeout','ambiguous_units')

@dataclass(frozen=True)
class CapabilityCase:
    case_id: str
    disposition: str
    frozen: AcceptanceObligationsV1
    rejected_response: str
    oracle_expression: str | None  # Diagnostic evaluator only; never model input.
    unit_sensitive: bool

def load_suite(path):
    raw=decode_strict_json(Path(path).read_text(encoding='utf-8'))
    if raw.get('suite_id')!='PUBLIC_INTERPRETATION_CAPABILITY_V1' or tuple(c['case_id'] for c in raw['cases'])!=CASES:
        raise ValueError('complete canonical five-case capability suite required')
    result=[]
    for c in raw['cases']:
        contract=WorkContract('capability-goal',c['case_id'],c['objective'],{},
            tuple(c['sources']),(),('candidate_diff',),Quota(2,1,1,0,0))
        frozen=build_acceptance_obligations(run_id='capability:'+c['case_id'],contract=contract,
            sources=c['sources'],tests={},public_contracts=c.get('public_contracts',{}))
        if c['disposition'] not in ('VALID_WITNESS','UNRESOLVED'):
            raise ValueError('unknown disposition')
        from .verification.acceptance import public_witness_binding, independent_obligation_refs
        proposal={'interpretations':{o.obligation_id:{'obligation_id':o.obligation_id,
            'expected_behavior':'Previous controlled concrete hypothesis.',
            'public_refs':sorted(independent_obligation_refs(frozen,o)),
            'unresolved':False,'witness':{**{k:public_witness_binding(frozen,o)[k] for k in ('module_path','callable_name')},
                'arguments':c['rejected_arguments'],'expected_expression':c['rejected_expression']}}
            for o in frozen.obligations if o.kind!='VERIFICATION'}}
        parse_public_interpretations(canonical(proposal),frozen)
        result.append(CapabilityCase(c['case_id'],c['disposition'],frozen,canonical(proposal),
            c.get('oracle_expression'),c['unit_sensitive']))
    return tuple(result)

def evaluate_response(case, raw, runner):
    outcome={'parser_valid':False,'grounding_valid':False,'obligations_complete':False,
        'witness_syntax_valid':False,'expression_valid':False,'domain_valid':False,
        'correct_unresolved':False,'semantic_disposition_correct':False,
        'false_concrete_interpretations':0,'wrong_unit_scale':0,'invalid_expression':0,'binding_failure':0}
    try:
        rows=parse_public_interpretations(raw,case.frozen)
        outcome.update(parser_valid=True,grounding_valid=True,obligations_complete=True,
            witness_syntax_valid=True,expression_valid=True)
        checked=validate_witness_inputs(case.frozen,rows,runner,decode=decode_strict_json)
        outcome['checked']=replace(case.frozen,public_interpretations=checked).to_dict()
        outcome['domain_valid']=bool(checked) and all(i.witness and i.input_validation
            and i.input_validation.status=='VALID' for i in checked)
        outcome['correct_unresolved']=all(i.unresolved and i.witness is None for i in checked)
        if case.disposition=='UNRESOLVED':
            outcome['false_concrete_interpretations']=sum(i.witness is not None or not i.unresolved for i in checked)
            outcome['semantic_disposition_correct']=outcome['correct_unresolved']
        else:
            supported=outcome['domain_valid'] and all(not i.unresolved for i in checked)
            relation=True
            for i in checked:
                if i.witness is None:relation=False;continue
                args=decode_strict_json(i.witness.arguments_json)['kwargs']
                expected=evaluate_public_reference(case.oracle_expression,args)
                # Independent, fixture-public arithmetic; original return is never read.
                if Decimal(i.witness.expected_text)!=Decimal(expected):relation=False
            outcome['public_relation_match']=relation
            outcome['semantic_disposition_correct']=bool(supported and relation)
            if case.unit_sensitive and any(i.witness is not None for i in checked) and (not outcome['domain_valid'] or not relation):
                outcome['wrong_unit_scale']=1
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        outcome['error']={'type':type(exc).__name__,'message':str(exc)[:2048]}
        message=str(exc).lower()
        outcome['binding_failure']=int('binding' in message or 'callable' in message)
        outcome['invalid_expression']=int('reference' in message or 'expression' in message)
    return outcome

def requests_for_case(case,runner):
    initial=public_interpretation_request(case.frozen)
    rejected=parse_public_interpretations(case.rejected_response,case.frozen)
    rejected=validate_witness_inputs(case.frozen,rejected,runner,decode=decode_strict_json)
    if not rejected or any(i.input_validation is None or i.input_validation.status!='INVALID' for i in rejected):
        raise ValueError('controlled repair fixture must be definitively INVALID')
    checked=replace(case.frozen,public_interpretations=rejected)
    repair=public_witness_repair_request(case.frozen,checked,
        proposal_fingerprint=fingerprint(case.rejected_response),attempt_index=1)
    return (('INITIAL_INTERPRETATION',initial),('DOMAIN_REPAIR',repair))

def summarize(samples):
    initial=[s for s in samples if s['capability']=='INITIAL_INTERPRETATION']
    repair=[s for s in samples if s['capability']=='DOMAIN_REPAIR']
    core=initial+repair
    rate=lambda rows,key:sum(bool(s['outcome'][key]) for s in rows)/len(rows) if rows else None
    unresolved=[s for s in core if s['expected_disposition']=='UNRESOLVED']
    supported_initial=[s for s in initial if s['expected_disposition']=='VALID_WITNESS']
    supported_repair=[s for s in repair if s['expected_disposition']=='VALID_WITNESS']
    return {'initial_parser_valid_rate':rate(initial,'parser_valid'),
        'initial_domain_valid_rate':rate(supported_initial,'domain_valid'),
        'repair_domain_valid_rate':rate(supported_repair,'domain_valid'),
        'correct_unresolved_rate':rate(unresolved,'correct_unresolved'),
        'false_concrete_interpretation_count':sum(s['outcome']['false_concrete_interpretations'] for s in core),
        'wrong_unit_scale_count':sum(s['outcome']['wrong_unit_scale'] for s in core),
        'invalid_expression_count':sum(s['outcome']['invalid_expression'] for s in core),
        'binding_failure_count':sum(s['outcome']['binding_failure'] for s in core),
        'initial_correct_count':sum(s['outcome']['semantic_disposition_correct'] for s in initial),
        'repair_correct_count':sum(s['outcome']['semantic_disposition_correct'] for s in repair),
        'mean_calls_to_terminal_interpretation':1.0 if core else None,
        'calls_to_terminal_note':'Independent one-call arms; failed samples are exhausted, not resolved.',
        'mean_latency_seconds':statistics.mean(s['latency_seconds'] for s in core) if core else None,
        'mean_generation_latency_seconds':statistics.mean(s.get('generation_wall_seconds',s['latency_seconds']) for s in core) if core else None,
        'qualified':len(initial)==len(repair)==5 and all(s['outcome']['parser_valid'] and s['outcome']['semantic_disposition_correct'] for s in core)}

class CapabilityScreen:
    def __init__(self,transport,profiles,cases,runner,*,common_repair=None,save=None,clock=time.perf_counter,resume=None,policy=None):
        self.transport,self.profiles,self.cases,self.runner=transport,profiles,cases,runner
        self.policy=policy or JudgePolicy();self.resume=resume
        self.clock=clock;self.save=save or (lambda r:None);self.common_repair=common_repair
        self.isolation=SequentialBenchmark(transport,tuple(profiles))
        self.report={'suite_id':'PUBLIC_INTERPRETATION_CAPABILITY_V1','diagnostic_only':True,
            'routing_changed':False,'candidate_executed':False,'hidden_read':False,'hidden_run':False,
            'arms':'one initial + one fixed controlled repair per case; no adaptive retries',
            'models':[],'selected_model':None,'fatal_error':None,'status':'IN_PROGRESS',
            'unload_diagnostics':self.isolation.report['unload_diagnostics']}
        if resume is not None:
            self.report=copy.deepcopy(resume)
            self.isolation.report['unload_diagnostics']=self.report['unload_diagnostics']
    def finalize_model(self,model):
        model['metrics']=summarize(model['samples'])
        common_samples=[s for s in model['samples'] if s['capability']=='E63_FIXED_DOMAIN_REPAIR']
        model['metrics']['e63_fixed_repair_correct']=all(s['outcome']['parser_valid'] and
            s['outcome']['semantic_disposition_correct'] for s in common_samples) if common_samples else None
        if common_samples:
            model['metrics']['qualified'] &= model['metrics']['e63_fixed_repair_correct']
        model['status']='QUALIFIED' if model['metrics']['qualified'] else 'NOT_QUALIFIED'
    def persist(self):self.save(self.report)
    def validate_resume(self,arms):
        if self.resume is None:return
        if self.report['status']!='STOPPED_RUNTIME_BOUNDARY' or self.report['routing_changed']:
            raise ValueError('only interrupted diagnostic screening may resume')
        ids=[m['model_id'] for m in self.report['models']]
        if ids!=[p.profile_id for p in self.profiles[:len(ids)]]:
            raise ValueError('resume model order mismatch')
        for model,profile in zip(self.report['models'],self.profiles):
            if model['profile']!=asdict(profile):raise ValueError('resume profile mismatch')
            if len(model['samples'])>len(arms):raise ValueError('resume sample count mismatch')
            for sample,(case,cap,(system,user,schema)) in zip(model['samples'],arms):
                if (sample['case_id']!=case.case_id or sample['capability']!=cap or
                    sample['snapshot_fingerprint']!=fingerprint(case.frozen.to_dict()) or
                    sample['request_fingerprint']!=fingerprint({'system':system,'user':user,'schema':schema}) or
                    sample['system_prompt']!=system or sample['user_prompt']!=user or
                    sample['response_contract']!=schema or sample['logical_model_id']!=profile.profile_id or
                    sample['concrete_model_tag']!=profile.model or 'outcome' not in sample):
                    raise ValueError('resume completed sample identity mismatch')
        self.report.setdefault('segments',[]).append({'previous_status':self.report['status'],
            'previous_fatal_error':self.report['fatal_error'],'retained_calls':sum(len(m['samples']) for m in self.report['models']),
            'rule':'only unstarted arms; terminal failures are never retried'})
        self.report['fatal_error']=None;self.report['status']='IN_PROGRESS'

    def run(self):
        # Prepare/validate all fixed prompts before any physical runtime work.
        plan=[(case,requests_for_case(case,self.runner)) for case in self.cases]
        arms=[(case,cap,request) for case,requests in plan for cap,request in requests]
        if self.common_repair is not None:
            arms.append((self.common_repair[0],'E63_FIXED_DOMAIN_REPAIR',self.common_repair[1]))
        self.validate_resume(arms)
        # Read-only exact identity preflight precedes unloading or generation.
        for model in self.report['models']:
            if model.get('digest'):
                profile=next(p for p in self.profiles if p.profile_id==model['model_id'])
                available=self.transport.request(profile.endpoint,'/api/tags')
                matches=[m for m in available['models'] if m.get('name')==profile.model and m.get('model',profile.model)==profile.model]
                if len(matches)!=1 or matches[0].get('digest')!=model['digest']:
                    raise ValueError('resume exact digest mismatch')
        try:
            residents=self.isolation.residents()
            if any(m.get('model',m.get('name')) not in MODELS.values() for _,m in residents):
                raise IsolationError('foreign initial residency; do not unload unrelated models')
            self.isolation.empty()
            for profile in self.profiles:
                model=next((m for m in self.report['models'] if m['model_id']==profile.profile_id),None)
                if model is not None and len(model['samples'])==len(arms):
                    self.finalize_model(model);self.persist();continue
                if model is not None and model.get('reason')=='exact model/tag/digest unavailable':continue
                if model is None:
                    model={'model_id':profile.profile_id,'profile':asdict(profile),'status':'NOT_RUN','samples':[]}
                    self.report['models'].append(model)
                self.persist()
                available=self.transport.request(profile.endpoint,'/api/tags')
                if not isinstance(available,dict) or not isinstance(available.get('models'),list):
                    raise IsolationError('malformed exact model availability')
                matches=[m for m in available['models'] if m.get('name')==profile.model and m.get('model',profile.model)==profile.model]
                model['availability']={'exact_tag':profile.model,'matches':matches}
                if len(matches)!=1 or not matches[0].get('digest'):
                    model['reason']='exact model/tag/digest unavailable';self.persist();continue
                digest=matches[0]['digest'];model['digest']=digest;model['status']='EXECUTING'
                for case,capability,(system,user,schema) in arms[len(model['samples']):]:
                    sample={'case_id':case.case_id,'capability':capability,'expected_disposition':case.disposition,
                        'calls':1,'raw_response':None,'snapshot_fingerprint':fingerprint(case.frozen.to_dict()),
                        'logical_role':'PUBLIC_INTERPRETATION','logical_model_id':profile.profile_id,
                        'concrete_model_tag':profile.model,'attempt_index':int(capability!='INITIAL_INTERPRETATION'),
                        'request_fingerprint':fingerprint({'system':system,'user':user,'schema':schema}),
                        'system_prompt':system,'user_prompt':user,'response_contract':schema}
                    model['samples'].append(sample)
                    payload={'model':profile.model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],
                        'format':ollama_compatible_schema(schema),'think':False,'stream':False,'keep_alive':-1,
                        'options':{'num_ctx':profile.num_ctx,'num_gpu':profile.num_gpu,'temperature':0,'num_predict':self.policy.output_tokens}}
                    sample['transport_policy']={'timeout_seconds':self.policy.timeout_seconds,'max_response_bytes':self.policy.max_response_bytes,'output_tokens':self.policy.output_tokens}
                    sample['request_payload']=payload;start=self.clock();response_received=False
                    try:
                        data=self.transport.request(profile.endpoint,'/api/chat',payload)
                        response_received=True
                        sample['generation_wall_seconds']=self.clock()-start
                        sample['response_model']=data.get('model')
                        if data.get('model')!=profile.model:raise IsolationError('returned model identity mismatch')
                        sample['observed_placement']=self.isolation.verify(profile,digest)
                        raw=data.get('message',{}).get('content')
                        if type(raw) is not str or len(raw.encode('utf-8'))>self.policy.max_response_bytes:
                            raise ValueError('missing/oversized structured response')
                        sample['raw_response']=raw
                        sample['outcome']=evaluate_response(case,raw,self.runner)
                    except IsolationError:raise
                    except Exception as exc:
                        sample['outcome']={'parser_valid':False,'grounding_valid':False,'obligations_complete':False,
                            'witness_syntax_valid':False,'expression_valid':False,'domain_valid':False,
                            'correct_unresolved':False,'semantic_disposition_correct':False,
                            'false_concrete_interpretations':0,'wrong_unit_scale':0,'invalid_expression':0,'binding_failure':0,
                            'error':{'type':type(exc).__name__,'message':str(exc)[:2048]}}
                        if not response_received:
                            raise IsolationError('generation transport failed; no subsequent request is safe') from exc
                    finally:
                        sample['latency_seconds']=self.clock()-start;self.persist()
                self.finalize_model(model)
                self.isolation.empty();self.persist()
            qualified=[m for m in self.report['models'] if m['status']=='QUALIFIED']
            qualified.sort(key=lambda m:(m['metrics']['mean_calls_to_terminal_interpretation'],m['metrics']['mean_generation_latency_seconds']))
            self.report['selected_model']=qualified[0]['model_id'] if qualified else None
            self.report['status']='CAPABILITY_QUALIFIED' if qualified else 'NO_LOCAL_MODEL_QUALIFIES_PUBLIC_INTERPRETER'
        except Exception as exc:
            self.report['fatal_error']={'type':type(exc).__name__,'message':str(exc)[:2048]}
            self.report['status']='STOPPED_RUNTIME_BOUNDARY'
            raise
        finally:self.persist()
        return self.report

def render(report):
    lines=['# PublicInterpreter capability screening',f"Status: **{report['status']}**.",
        'Five public-only cases; independent initial and fixed repair arms. Both arms must pass 5/5. Small capability screening, not statistical reliability proof. No routing change. Final Judge remains Ornith.',
        '| Model | Availability | Initial dispositions | Repair dispositions | Correct unresolved | Wrong units | Qualified |',
        '|---|---|---|---|---|---|---|']
    for m in report['models']:
        s=m.get('metrics',{})
        lines.append(f"| {m['model_id']} | {m['status']} | {s.get('initial_correct_count')}/5 | {s.get('repair_correct_count')}/5 | {s.get('correct_unresolved_rate')} | {s.get('wrong_unit_scale_count')} | {s.get('qualified',False)} |")
    for m in report['models']:
        lines.extend(['',f"Exact tag `{m['profile']['model']}`; digest `{m.get('digest')}`; configured placement `{m['profile']}`.",''])
        for sample in m['samples']:
            lines.append(f"- {sample['case_id']} / {sample['capability']}: parser={sample['outcome']['parser_valid']}, domain={sample['outcome']['domain_valid']}, semantic disposition={sample['outcome']['semantic_disposition_correct']}; calls=1; wall={sample['latency_seconds']:.3f}s. Raw response: companion JSON model `{m['model_id']}`, sample `{sample['case_id']}/{sample['capability']}`.")
    lines+=['',f"Selected: {report['selected_model']}. Required gates: initial 5/5, fixed repair 5/5, all outputs parser-valid, no unsafe concrete abstention cases, plus the exact E6.3 repair fixture. E6 historical qualification remains 1/3 NOT QUALIFIED; no sample reused."]
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');p.add_argument('--suite',type=Path,required=True)
    p.add_argument('--common-repair-fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resume',action='store_true')
    args=p.parse_args()
    if not args.live:p.error('--live required')
    if args.output.exists() and not args.resume:raise FileExistsError(args.output)
    resume=decode_strict_json(args.output.read_text(encoding='utf-8')) if args.resume else None
    cases=load_suite(args.suite);root=Path.cwd()
    profiles=tuple(profiles_from_runtime(root,'production',model_id=m)[0] for m in MODELS)
    if any(p.model!=MODELS[p.profile_id] or p.endpoint.rstrip('/')!='http://127.0.0.1:11435' for p in profiles):
        raise ValueError('exact model profile/endpoint mismatch; no substitution')
    runner=VerificationRunner(manager=None,policy=VerificationPolicy(trusted_python_executable=str(root/'.venv/Scripts/python.exe')))
    common=decode_strict_json(args.common_repair_fixture.read_text(encoding='utf-8'))
    frozen=AcceptanceObligationsV1.from_dict(common['frozen'])
    checked=AcceptanceObligationsV1.from_dict(common['checked'])
    common_case=CapabilityCase('e63_rejected_proposal','VALID_WITNESS',frozen,'',common['oracle_expression'],True)
    request=public_witness_repair_request(frozen,checked,proposal_fingerprint=common['proposal_fingerprint'],attempt_index=1)
    def save(r):
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
        args.output.with_suffix('.md').write_text(render(r) if all('outcome' in s for m in r['models'] for s in m['samples']) else 'IN_PROGRESS\n',encoding='utf-8')
    policy=JudgePolicy.from_policies(ConfigLoader(root).load_policies())
    report=CapabilityScreen(NativeTransport(timeout=policy.timeout_seconds),profiles,cases,runner,common_repair=(common_case,request),save=save,resume=resume,policy=policy).run()
    print(json.dumps({'status':report['status'],'selected_model':report['selected_model']}))
if __name__=='__main__':main()
