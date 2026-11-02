"""Original-input admissibility only; never an expected-output oracle."""
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import re


@dataclass(frozen=True, slots=True)
class PublicWitnessInputValidationV1:
    status: str
    reason: str
    run_id: str
    todo_id: str
    public_snapshot_fingerprint: str
    obligation_id: str
    public_refs: tuple[str, ...]
    origin_path: str
    origin_symbol: str
    signature_fingerprint: str
    arguments_fingerprint: str
    exception_class: str = ''

    def __post_init__(self):
        if self.status not in ('VALID','INVALID','UNRESOLVED'):
            raise ValueError('invalid witness-domain status')
        if self.reason not in ('DECLARED_INPUT_ADMISSIBLE','ORIGINAL_CALL_ADMISSIBLE','ORIGINAL_CALL_NOT_ADMISSIBLE',
                'CALLABLE_BINDING_INVALID','VALIDATION_NOT_APPLICABLE','ORIGINAL_PROBE_UNAVAILABLE'):
            raise ValueError('invalid witness-domain reason')
        if type(self.public_refs) is not tuple or any(type(x) is not str for x in self.public_refs):
            raise ValueError('invalid domain public references')
        if any(type(getattr(self,name)) is not str for name in (
                'reason','run_id','todo_id','public_snapshot_fingerprint','obligation_id',
                'origin_path','origin_symbol','signature_fingerprint','arguments_fingerprint','exception_class')):
            raise ValueError('invalid domain identity')
        if len(self.exception_class)>128 or (self.exception_class and not self.exception_class.isidentifier()):
            raise ValueError('invalid exception class')
        if (self.status=='VALID' and (self.reason not in ('ORIGINAL_CALL_ADMISSIBLE','DECLARED_INPUT_ADMISSIBLE') or self.exception_class)
                or self.status=='INVALID' and (self.reason!='ORIGINAL_CALL_NOT_ADMISSIBLE' or not self.exception_class)
                or self.status=='UNRESOLVED' and self.reason in ('ORIGINAL_CALL_ADMISSIBLE','ORIGINAL_CALL_NOT_ADMISSIBLE')):
            raise ValueError('inconsistent witness-domain disposition')

    def to_dict(self):
        return asdict(self)


def preserved_validation_contract(frozen):
    """Recognize an explicit preservation instruction, not inferred intent.

    Other mentions of validation/range/coercion/exception/parser acceptance
    make applicability unresolved. This deliberately supports a narrow contract.
    """
    from .acceptance import SemanticProvenanceV1 as P
    texts=[e.content.lower() for e in frozen.evidence if e.provenance in
           (P.AUTHORITATIVE_REQUIREMENT,P.PUBLIC_CONTRACT_EVIDENCE)]
    preservation=r'\b(?:keep|preserve) (?:existing |public |input )?validation\b'
    if not any(re.search(preservation,t) for t in texts):
        return False
    remainder='\n'.join(re.sub(preservation,'',t) for t in texts)
    return not re.search(r'\b(?:validation|(?:input|accepted|rejected) range|exception behavior|type coercion|parser acceptance)\b',remainder)


# Discard return values in the child process. Only admissibility is observable.
_SCRIPT=r'''
import sys,json,decimal,math,fractions,contextlib,types
class Sink:
    def write(self,value): return len(value)
    def flush(self): pass
rows=[]
for call in json.loads(sys.argv[1]):
    error=''
    try:
        with contextlib.redirect_stdout(Sink()),contextlib.redirect_stderr(Sink()):
            for dependency in call.get('local_modules', []):
                module=types.ModuleType(dependency['name'])
                sys.modules[dependency['name']]=module
                exec(compile(dependency['source'],'<frozen-public-dependency>','exec'),module.__dict__)
            scope={'__name__':'public_original'}
            exec(compile(call['source'],'<frozen-public-original>','exec'),scope)
            scope[call['symbol']](*call['args'],**call['kwargs'])
    except Exception as exc:
        error=type(exc).__name__
    rows.append({'key':call['key'],'exception_class':error})
print(json.dumps(rows,allow_nan=False))
'''


def validate_witness_inputs(frozen, interpretations, runner, *, decode):
    from .callable_authority import from_frozen, declaration_for, validate_arguments
    if from_frozen(frozen) is not None:
        from .acceptance import fingerprint, validate_witness_binding
        rows = []
        for item in interpretations:
            if item.witness is None or item.unresolved:
                raise ValueError('PUBLIC_AUTHORITY_MANDATORY_WITNESS_MISSING')
            witness = item.witness
            obligation = next(o for o in frozen.obligations if o.obligation_id == item.obligation_id)
            binding = validate_witness_binding(frozen,obligation,witness.module_path,witness.callable_name)
            arguments = decode(witness.arguments_json)
            validate_arguments(declaration_for(frozen,witness.module_path,witness.callable_name),arguments,
                witness.expected_expression,witness.expected_exception)
            validation = PublicWitnessInputValidationV1('VALID','DECLARED_INPUT_ADMISSIBLE',
                frozen.run_id,frozen.todo_id,fingerprint(replace(frozen,public_interpretations=()).to_dict()),
                item.obligation_id,item.public_refs,witness.module_path,witness.callable_name,
                binding['signature_fingerprint'],fingerprint(arguments))
            rows.append(replace(item,input_validation=validation))
        return tuple(rows)
    from .acceptance import (fingerprint,canonical,public_witness_binding,
        validate_witness_binding,public_signature,public_witness_requires_boolean,SemanticProvenanceV1 as P)
    from .public_witness import qualify_public_module,qualify_frozen_public_closure
    from .acceptance import public_group_limit
    from .models import (CommandCatalogEntry,VerificationGateType,VerificationGateSpec,
        VerificationGateKind,GateOutcome)
    from .runner import VerificationRunner
    from .errors import VerificationPolicyError
    snapshot=fingerprint(replace(frozen,public_interpretations=()).to_dict())
    results={}; calls={}
    applicable=preserved_validation_contract(frozen)
    for item in interpretations:
        witness=item.witness
        if witness is None or item.unresolved:
            continue
        obligation=next(o for o in frozen.obligations if o.obligation_id==item.obligation_id)
        binding=public_witness_binding(frozen,obligation)
        identity=dict(run_id=frozen.run_id,todo_id=frozen.todo_id,
            public_snapshot_fingerprint=snapshot,obligation_id=item.obligation_id,
            public_refs=item.public_refs,origin_path=(binding or {}).get('module_path',''),
            origin_symbol=(binding or {}).get('callable_name',''),
            signature_fingerprint=(binding or {}).get('signature_fingerprint',''),
            arguments_fingerprint=fingerprint(decode(witness.arguments_json)))
        status,reason='UNRESOLVED','VALIDATION_NOT_APPLICABLE'
        try:
            validate_witness_binding(frozen,obligation,witness.module_path,witness.callable_name)
            if binding is None:
                raise ValueError('missing origin')
            boolean_contract = public_witness_requires_boolean(frozen,obligation)
            multi=public_group_limit(frozen)==3
            if applicable or boolean_contract or multi:
                source=next(e.content for e in frozen.evidence
                    if e.provenance is P.OBSERVED_IMPLEMENTATION and e.path==binding['module_path'])
                local_modules=[]
                if multi:
                    sources={e.path:e.content for e in frozen.evidence if e.provenance is P.OBSERVED_IMPLEMENTATION}
                    tree,local_modules=qualify_frozen_public_closure(sources,binding['module_path'],binding['callable_name'])
                else:tree=qualify_public_module(source,binding['callable_name'])
                import ast
                targets=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==binding['callable_name']]
                if len(targets)!=1 or fingerprint(public_signature(targets[0]))!=binding['signature_fingerprint']:
                    raise ValueError('original signature mismatch')
                arguments=decode(witness.arguments_json)
                variants=[arguments]
                if boolean_contract:
                    from itertools import product
                    names=[a.arg for a in targets[0].args.args]
                    if (len(names)!=2 or arguments['args'] or set(arguments['kwargs'])!=set(names)
                            or any(type(v) is not bool for v in arguments['kwargs'].values())):
                        raise ValueError('unsupported Boolean input domain')
                    variants=[{'args':[],'kwargs':dict(zip(names,values))} for values in product((False,True),repeat=2)]
                keys=[]
                for variant in variants:
                    key=fingerprint({'source':source,'symbol':binding['callable_name'],'arguments':variant,
                        **({'local_modules':local_modules} if local_modules else {})})
                    calls[key]={'key':key,'source':source,'symbol':binding['callable_name'],**variant,
                        **({'local_modules':local_modules} if local_modules else {})}
                    keys.append(key)
                status,reason='UNRESOLVED','ORIGINAL_PROBE_UNAVAILABLE'
                identity['_keys']=keys
        except (ValueError,SyntaxError,StopIteration):
            status,reason='UNRESOLVED','CALLABLE_BINDING_INVALID'
        results[item.obligation_id]=(status,reason,identity)
    rows={}
    if calls and runner is not None:
        catalog='public_witness_input_domain'
        entry=CommandCatalogEntry(catalog,VerificationGateType.CUSTOM,
            ('{python}','-I','-B','-c',_SCRIPT,canonical(list(calls.values()))),min(30,runner.policy.max_gate_timeout_seconds))
        policy=replace(runner.policy,max_output_bytes=min(runner.policy.max_output_bytes,8192),
            command_catalog={**runner.policy.command_catalog,catalog:entry})
        try:
            # Frozen source is carried in argv, not read from a repository cwd.
            # Keep the trusted interpreter outside the harness-owned execution root.
            with TemporaryDirectory(prefix='swarm-public-original-probe-') as scratch:
                result=VerificationRunner(manager=runner.manager,policy=policy)._command(
                    VerificationGateSpec(catalog,VerificationGateKind.COMMAND,VerificationGateType.CUSTOM,True,catalog_id=catalog),
                    Path(scratch).resolve(strict=True),max_seconds=30)
            if result.outcome is GateOutcome.PASSED and not result.output_truncated:
                raw=decode(result.stdout)
                if (type(raw) is list and len(raw)==len(calls) and
                    {r['key'] for r in raw}==set(calls) and all(set(r)=={'key','exception_class'}
                    and type(r['exception_class']) is str and len(r['exception_class'])<=128 for r in raw)):
                    rows={r['key']:r['exception_class'] for r in raw}
        except (VerificationPolicyError,ValueError,TypeError,KeyError,OSError):
            pass
    output=[]
    for item in interpretations:
        if item.obligation_id not in results:
            output.append(item);continue
        status,reason,identity=results[item.obligation_id]
        keys=identity.pop('_keys',[]); error=next((rows[k] for k in keys if k in rows and rows[k]),'')
        if keys and all(k in rows for k in keys):
            status,reason=('INVALID','ORIGINAL_CALL_NOT_ADMISSIBLE') if error else ('VALID','ORIGINAL_CALL_ADMISSIBLE')
        validation=PublicWitnessInputValidationV1(status,reason,**identity,exception_class=error or '')
        output.append(replace(item,input_validation=validation))
    return tuple(output)
