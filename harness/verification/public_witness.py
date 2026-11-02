"""Bounded public counterexamples, using the existing trusted command runner.

Expected values are frozen model hypotheses, not specifications. A match only
removes this counterexample; the final Judge still must establish entailment.
This restricted Python subset is an eligibility check, not a general sandbox.
"""
from dataclasses import asdict, dataclass, replace
import ast
from pathlib import Path

from .acceptance import (canonical, public_callable_surface, public_signature,
                         fingerprint, validate_witness_binding, public_witness_binding)
from .models import (CommandCatalogEntry, GateOutcome, VerificationGateKind,
                     VerificationGateSpec, VerificationGateType)
from .runner import VerificationRunner
from .errors import VerificationPolicyError
from .public_reference import evaluate_public_reference_value
from .acceptance import public_witness_requires_boolean
from ..workspace.errors import WorkspaceError


@dataclass(frozen=True, slots=True)
class PublicWitnessObservationV1:
    obligation_id: str
    status: str
    observed_text: str = ''
    detail: str = ''
    evidence_id: str = ''
    origin_path: str = ''
    origin_symbol: str = ''
    origin_signature_fingerprint: str = ''
    binding_source: str = ''
    model_proposed_callable: str = ''
    binding_mismatch: bool = False
    candidate_resolution: str = 'UNRESOLVED'
    expected_text: str = ''

    def __post_init__(self):
        if self.status not in ('MATCH', 'MISMATCH', 'UNRESOLVED'):
            raise ValueError('invalid witness observation status')
        if any(type(v) is not str for v in (self.obligation_id, self.observed_text, self.detail)):
            raise ValueError('witness observation fields must be strings')
        if not self.obligation_id or len(self.observed_text.encode()) > 4096 or len(self.detail.encode()) > 2048:
            raise ValueError('witness observation exceeds bounds')
        if type(self.binding_mismatch) is not bool or any(type(getattr(self, field)) is not str for field in
                ('evidence_id','origin_path','origin_symbol','origin_signature_fingerprint','binding_source',
                 'model_proposed_callable','candidate_resolution','expected_text')):
            raise ValueError('invalid witness binding diagnostics')

    def to_dict(self):
        return asdict(self)


_IMPORTS = {'decimal', 'math', 'fractions'}
_BUILTINS = {'str', 'int', 'float', 'bool', 'len', 'sum', 'min', 'max', 'abs',
             'round', 'enumerate', 'range', 'zip', 'list', 'tuple', 'dict',
             'ValueError', 'TypeError', 'ArithmeticError'}
_METHODS = {'quantize', 'get', 'append', 'extend', 'copy', 'count', 'index', 'strip', 'casefold', 'lower', 'upper'}


def qualify_public_module(source, callable_name, *, local_imports=None):
    """Conservative eligibility: unsupported programs are never executed."""
    tree = ast.parse(source)
    if '.' in callable_name:
        from .callable_authority import observe_interface
        observe_interface(source,'eligible.py',callable_name,'INSTANCE_METHOD',candidate=True)
        # Reuse the existing pure-function qualifier over method bodies. The
        # fixed runner alone constructs the statically proven zero-arg receiver.
        flattened = []
        for node in tree.body:
            if not isinstance(node,ast.ClassDef):
                flattened.append(node)
                continue
            if node.bases or node.keywords or node.decorator_list:
                raise ValueError('unsupported public witness class')
            for method in node.body:
                if isinstance(method,ast.Expr) and isinstance(method.value,ast.Constant) and type(method.value.value) is str:
                    continue
                if not isinstance(method,ast.FunctionDef) or method.name.startswith('_') or method.decorator_list:
                    raise ValueError('unsupported public witness class body')
                import copy
                projected = copy.deepcopy(method)
                projected.name = node.name+'_'+method.name
                flattened.append(projected)
        projected = ast.Module(body=flattened,type_ignores=[])
        qualify_public_module(ast.unparse(projected),callable_name.replace('.','_'),local_imports=local_imports)
        return tree
    names = set(_BUILTINS)
    functions = set()
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
            continue
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise ValueError('decorated public functions are unsupported')
            for default in (*node.args.defaults, *(v for v in node.args.kw_defaults if v is not None)):
                ast.literal_eval(default)
            functions.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            if (node.level or any(a.name == '*' for a in node.names)
                    or node.module not in _IMPORTS and (local_imports is None or node.module not in local_imports
                        or any(a.name not in local_imports[node.module] for a in node.names))):
                raise ValueError('public witness requires pure standard-library imports')
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            if any(a.name not in _IMPORTS for a in node.names):
                raise ValueError('public witness requires pure standard-library imports')
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            ast.literal_eval(node.value)
        else:
            raise ValueError('unsupported executable module-level statement')
    if callable_name not in functions:
        raise ValueError('PUBLIC_WITNESS_TARGET_MISSING: public function missing in candidate')
    names.update(functions)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body:
            raise ValueError('nested imports are unsupported')
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Lambda, ast.Global, ast.Nonlocal,
                             ast.Yield, ast.YieldFrom, ast.Await, ast.Delete)):
            raise ValueError('unsupported public witness syntax')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('reflective access is unsupported')
        if isinstance(node, ast.Attribute) and (node.attr.startswith('_') or node.attr not in _METHODS):
            raise ValueError('unsupported public witness attribute')
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in names:
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in _METHODS:
                continue
            raise ValueError('unsupported public witness call')
    return tree


def qualify_public_closure(workspace, module_path, callable_name, *, include_source=False):
    """Authorize and qualify bounded flat local function imports before execution.

    This does not broaden the model reference language or accept arbitrary
    repository imports. Every dependency is read through Workspace, qualified
    under the same pure-module rules, and bound to its actual authorized path.
    """
    from pathlib import PurePosixPath
    dependencies=[]
    visiting=set()
    qualified={}
    size=0
    def visit(path, symbol):
        nonlocal size
        if path in visiting:
            raise ValueError('cyclic public witness imports are unsupported')
        if path in qualified:
            if symbol not in qualified[path]:
                raise ValueError('public dependency function missing')
            return
        visiting.add(path)
        _,source=workspace.read_text(path)
        size+=len(source.encode('utf-8'))
        if size>32768 or len(visiting)+len(qualified)>4:
            raise ValueError('public dependency closure exceeds bounds')
        tree=ast.parse(source);local={}
        for node in tree.body:
            if isinstance(node,ast.ImportFrom) and node.module not in _IMPORTS:
                if node.level or not node.module or not node.module.isidentifier() or node.module.startswith('_'):
                    raise ValueError('only flat local function imports are supported')
                dependency=(PurePosixPath(path).parent/(node.module+'.py')).as_posix()
                for alias in node.names:
                    visit(dependency,alias.name)
                local[node.module]=qualified[dependency]
                _,target=workspace._candidate(dependency)
                row={'name':node.module,'path':str(target)}
                if include_source:row['source']=workspace.read_text(dependency)[1]
                if row not in dependencies:dependencies.append(row)
        qualify_public_module(source,symbol,local_imports=local)
        qualified[path]={n.name for n in tree.body if isinstance(n,ast.FunctionDef)}
        visiting.remove(path)
    visit(module_path,callable_name)
    _,source=workspace.read_text(module_path)
    return ast.parse(source),dependencies


def qualify_frozen_public_closure(sources,module_path,callable_name):
    """The same qualifier over immutable pre-mutation bytes, with no file IO."""
    from pathlib import PurePosixPath
    class FrozenReader:
        def read_text(self,path):
            if path not in sources:
                raise ValueError('missing frozen public dependency')
            return path,sources[path]
        def _candidate(self,path):
            self.read_text(path)
            return path,PurePosixPath(path)
    return qualify_public_closure(FrozenReader(),module_path,callable_name,include_source=True)


# Fixed harness code. No model-provided source or expected result reaches it.
# Import the allowed standard library before the candidate can shadow modules.
_SCRIPT = r'''
import sys, json, decimal, math, fractions, importlib.util, contextlib
class Sink:
    def write(self, value): return len(value)
    def flush(self): pass
rows = []
for call in json.loads(sys.argv[1]):
    try:
        with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
            for dependency in call.get('local_modules', []):
                dependency_spec = importlib.util.spec_from_file_location(dependency['name'], dependency['path'])
                dependency_module = importlib.util.module_from_spec(dependency_spec)
                sys.modules[dependency['name']] = dependency_module
                dependency_spec.loader.exec_module(dependency_module)
                delegation = call.get('delegation')
                if delegation and dependency['name'] == delegation['module_name']:
                    original = getattr(dependency_module,delegation['symbol'])
                    delegated_calls = []
                    delegation_probe = False
                    def traced(*args, **kwargs):
                        delegated_calls.append({'args':list(args),'kwargs':kwargs})
                        if delegation_probe:
                            return 1000003
                        return original(*args,**kwargs)
                    setattr(dependency_module,delegation['symbol'],traced)
            spec = importlib.util.spec_from_file_location('public_candidate', call['module_path'])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            parts = call['callable_name'].split('.')
            target = getattr(module,parts[0])
            if len(parts) == 2:
                target = getattr(target(),parts[1])
            value = target(*call['args'], **call['kwargs'])
            text = str(value)
        if len(text.encode('utf-8')) > 4096: raise ValueError('result exceeds bound')
        row = {'obligation_id':call['obligation_id'], 'observed_text':text, 'error':None}
        if call.get('delegation'):
            initial_delegation = bool(delegated_calls)
            delegated_calls.clear()
            delegation_probe = True
            with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
                probe_value = target(*call['args'], **call['kwargs'])
            row['delegated'] = initial_delegation and bool(delegated_calls) and type(probe_value) is int and probe_value == 1000003
        if call.get('require_typed'):
            row['observed_typed']={'type':type(value).__name__,'json':json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))}
        if call.get('require_boolean'):
            row['observed_boolean'] = type(value) is bool
            observations = []
            for kwargs in call['boolean_inputs']:
                with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
                    result = getattr(module, call['callable_name'])(**kwargs)
                    observations.append({'text':str(result), 'is_boolean':type(result) is bool})
            row['boolean_observations'] = observations
        rows.append(row)
    except Exception as exc:
        rows.append({'obligation_id':call['obligation_id'], 'observed_text':'', 'error':type(exc).__name__})
print(json.dumps(rows, ensure_ascii=False, allow_nan=False))
'''


class PublicWitnessRunner:
    def __init__(self, verification_runner):
        self.runner = verification_runner

    def evaluate(self, *, frozen, workspace, decode):
        # Only a frozen integration-issued accepted dependency may extend the
        # verifier's read view outside this Todo's mutation scope.
        from .callable_authority import VerifiedAcceptedDependencyV1,public_read_projection
        for evidence in frozen.evidence:
            if evidence.evidence_id.startswith('dependency_interface:'):
                revision=decode(evidence.content).get('accepted_revision')
                if type(revision) is dict:
                    proof=VerifiedAcceptedDependencyV1(canonical(revision)).to_dict()
                    workspace=public_read_projection(workspace.session,proof['source_sha256'])
                    for path,expected in proof['source_sha256'].items():
                        if __import__('hashlib').sha256(workspace.read_text(path)[1].encode('utf8')).hexdigest()!=expected:
                            raise ValueError('PUBLIC_AUTHORITY_ACCEPTED_DEPENDENCY_SOURCE_CHANGED')
        obligations = tuple(o for o in frozen.obligations if o.kind != 'VERIFICATION')
        missing = {o.obligation_id:PublicWitnessObservationV1(o.obligation_id, 'UNRESOLVED',
            detail='No eligible candidate-independent public witness') for o in obligations}
        from .acceptance import public_group_limit
        from .callable_authority import from_frozen, declaration_for, observe_interface
        declared = from_frozen(frozen) is not None
        if frozen.unresolved_semantics or len(obligations) > (12 if public_group_limit(frozen)==3 else 4):
            return tuple(missing.values())
        known = {(s['module_path'], s['callable_name']) for s in public_callable_surface(frozen)}
        calls = []
        expected = {}
        typed_expected = {}
        boolean_cases = set()
        boolean_expected = {}
        exception_expected = {}
        delegation_cases = set()
        shared = {}
        members = {}
        for interpretation in frozen.public_interpretations:
            witness = interpretation.witness
            if interpretation.unresolved or witness is None or interpretation.obligation_id not in missing:
                continue
            domain=interpretation.input_validation
            if domain is not None:
                if (domain.status!='VALID' or domain.run_id!=frozen.run_id or domain.todo_id!=frozen.todo_id
                        or domain.obligation_id!=interpretation.obligation_id
                        or domain.public_snapshot_fingerprint!=fingerprint(replace(frozen,public_interpretations=()).to_dict())
                        or domain.arguments_fingerprint!=fingerprint(decode(witness.arguments_json))
                        or domain.public_refs!=interpretation.public_refs):
                    missing[interpretation.obligation_id]=replace(missing[interpretation.obligation_id],
                        detail='Witness input domain '+domain.reason,candidate_resolution='INPUT_DOMAIN_UNRESOLVED')
                    continue
            try:
                obligation = next(o for o in obligations if o.obligation_id == interpretation.obligation_id)
                binding = public_witness_binding(frozen, obligation)
                if binding is None:
                    raise ValueError('PUBLIC_WITNESS_BINDING_UNRESOLVED')
                if domain is not None and (domain.origin_path,domain.origin_symbol,domain.signature_fingerprint)!= (
                        binding['module_path'],binding['callable_name'],binding['signature_fingerprint']):
                    raise ValueError('PUBLIC_WITNESS_DOMAIN_BINDING_MISMATCH')
                missing[interpretation.obligation_id] = replace(missing[interpretation.obligation_id],
                    evidence_id=binding['evidence_id'],origin_path=binding['module_path'],
                    origin_symbol=binding['callable_name'],origin_signature_fingerprint=binding['signature_fingerprint'],
                    binding_source=binding['binding_source'],model_proposed_callable=witness.callable_name,
                    expected_text=witness.expected_text)
                validate_witness_binding(frozen, obligation, witness.module_path, witness.callable_name)
                if (witness.module_path, witness.callable_name) not in known:
                    raise ValueError('witness is not bound to original public API')
                _, source = workspace.read_text(witness.module_path)
                tree,local_modules = qualify_public_closure(workspace,witness.module_path,witness.callable_name)
                declaration = declaration_for(frozen,witness.module_path,witness.callable_name) if declared else None
                if declared:
                    observed = observe_interface(source,witness.module_path,witness.callable_name,declaration['kind'],candidate=True)
                    if not observed.present or observed.signature_fingerprint != binding['signature_fingerprint']:
                        raise ValueError('PUBLIC_WITNESS_SIGNATURE_MISMATCH')
                    if domain is None or domain.reason != 'DECLARED_INPUT_ADMISSIBLE':
                        raise ValueError('PUBLIC_WITNESS_DECLARED_DOMAIN_UNQUALIFIED')
                else:
                    targets = [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name == binding['callable_name']]
                    if len(targets) != 1 or fingerprint(public_signature(targets[0])) != binding['signature_fingerprint']:
                        raise ValueError('PUBLIC_WITNESS_SIGNATURE_MISMATCH')
                missing[interpretation.obligation_id] = replace(missing[interpretation.obligation_id], candidate_resolution='RESOLVED')
                arguments = decode(witness.arguments_json)
                if type(arguments) is not dict or set(arguments) != {'args', 'kwargs'} or type(arguments['args']) is not list or type(arguments['kwargs']) is not dict:
                    raise ValueError('invalid witness arguments')
                require_boolean = public_witness_requires_boolean(frozen, obligation)
                reference_value = None
                boolean_inputs = []
                if witness.expected_expression is not None:
                    surface = next(s for s in public_callable_surface(frozen)
                        if (s['module_path'],s['callable_name']) == (witness.module_path,witness.callable_name))
                    variables = {**dict(zip(surface['positional_parameters'],arguments['args'])), **arguments['kwargs']}
                    reference_value = evaluate_public_reference_value(witness.expected_expression,variables,
                        required_names=[n for n,v in surface['literal_defaults'].items() if type(v) in (int,float)])
                    from .public_witness_contract import callable_witness_contract
                    typed_contract=callable_witness_contract(frozen,surface)
                    typed_contract.validate(variables,witness.expected_expression)
                    from .public_reference import PublicReferenceExceptionV1
                    if declared and isinstance(reference_value,PublicReferenceExceptionV1):
                        if witness.expected_exception != reference_value.exception_class:
                            raise ValueError('PUBLIC_WITNESS_EXCEPTION_MISMATCH')
                        exception_expected[interpretation.obligation_id] = witness.expected_exception
                    if typed_contract.kind!='LEGACY':
                        import json
                        if interpretation.obligation_id not in exception_expected:
                            typed_expected[interpretation.obligation_id]={'type':type(reference_value).__name__,'json':json.dumps(reference_value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))}
                    if require_boolean:
                        names = surface['parameters']
                        if len(names) != 2 or any(type(v) is not bool for v in variables.values()):
                            raise ValueError('Boolean witness supports exactly two Boolean parameters')
                        from itertools import product
                        boolean_inputs = [dict(zip(names, values)) for values in product((False,True),repeat=2)]
                        boolean_expected[interpretation.obligation_id] = [str(evaluate_public_reference_value(
                            witness.expected_expression, values, required_names=names)) for values in boolean_inputs]
                    if str(reference_value) != witness.expected_text:
                        raise ValueError('frozen reference expression/result mismatch')
                if require_boolean and type(reference_value) is not bool:
                    raise ValueError('public Boolean contract requires a Boolean reference result')
                if require_boolean or type(reference_value) is bool:
                    boolean_cases.add(interpretation.obligation_id)
                # Authorize the effective destination at the central boundary.
                _, path = workspace._candidate(witness.module_path)
                delegation = None
                if declared and declaration['delegation'] is not None:
                    dep = declaration['delegation']
                    module_name = Path(dep['module_path']).stem
                    if not any(d['name'] == module_name for d in local_modules):
                        raise ValueError('PUBLIC_WITNESS_REQUIRED_DELEGATION_MISSING')
                    delegation = {'module_name':module_name,'symbol':dep['qualified_name']}
                    delegation_cases.add(interpretation.obligation_id)
                # Per-candidate execution batch shares only an identical frozen
                # origin + arguments/reference case. Outer Judge cache also
                # binds root snapshot and candidate head/diff.
                key = fingerprint({'root':fingerprint(frozen.to_dict()), 'binding':{
                    k:binding[k] for k in ('module_path','callable_name','signature_fingerprint')},
                    'arguments':arguments,'reference':witness.expected_expression,'expected':witness.expected_text,
                    'require_boolean':require_boolean})
                representative = shared.get(key)
                if representative is None:
                    representative = interpretation.obligation_id
                    shared[key] = representative
                    calls.append({'obligation_id':representative,
                        'module_path':str(path), 'callable_name':binding['callable_name'], **arguments,
                        **({'require_typed':True} if interpretation.obligation_id in typed_expected else {}),
                        **({'local_modules':local_modules} if local_modules else {}),
                        **({'delegation':delegation} if delegation is not None else {}),
                        **({'require_boolean':True,'boolean_inputs':boolean_inputs} if require_boolean else
                            {'require_boolean':True,'boolean_inputs':[]} if interpretation.obligation_id in boolean_cases else {})})
                    members[representative] = []
                members[representative].append(interpretation.obligation_id)
                expected[interpretation.obligation_id] = witness.expected_text
            except (ValueError, SyntaxError, OSError, WorkspaceError) as exc:
                detail = str(exc)[:512]
                missing[interpretation.obligation_id] = replace(missing[interpretation.obligation_id],
                    detail=detail, model_proposed_callable=witness.callable_name,
                    binding_mismatch='BINDING_MISMATCH' in detail,
                    candidate_resolution='INVALID')
        if not calls:
            return tuple(missing.values())
        catalog_id = 'public_acceptance_witness'
        entry = CommandCatalogEntry(catalog_id, VerificationGateType.CUSTOM,
            ('{python}', '-I', '-B', '-c', _SCRIPT, canonical(calls)), min(30,self.runner.policy.max_gate_timeout_seconds))
        policy = replace(self.runner.policy, max_output_bytes=min(self.runner.policy.max_output_bytes, 16384),
            command_catalog={**self.runner.policy.command_catalog, catalog_id:entry})
        runner = VerificationRunner(manager=self.runner.manager, policy=policy)
        gate = VerificationGateSpec(catalog_id, VerificationGateKind.COMMAND, VerificationGateType.CUSTOM, True, catalog_id=catalog_id)
        try:
            result = runner._command(gate, Path(workspace.root), max_seconds=30)
        except VerificationPolicyError as exc:
            return tuple(PublicWitnessObservationV1(o.obligation_id,'UNRESOLVED',detail=str(exc)[:512]) for o in obligations)
        if result.outcome is not GateOutcome.PASSED or result.output_truncated:
            return tuple(PublicWitnessObservationV1(o.obligation_id, 'UNRESOLVED',
                detail='Public witness command unavailable: '+result.detail[:512]) for o in obligations)
        try:
            rows = decode(result.stdout)
            if type(rows) is not list or len(rows) != len(calls):
                raise ValueError('invalid public witness observation count')
            seen = set()
            for row in rows:
                required_keys={'obligation_id','observed_text','error'}
                if row.get('error') is None:
                    if row.get('obligation_id') in boolean_cases:required_keys|={'observed_boolean','boolean_observations'}
                    if row.get('obligation_id') in typed_expected:required_keys.add('observed_typed')
                    if row.get('obligation_id') in delegation_cases:required_keys.add('delegated')
                if type(row) is not dict or set(row) != required_keys:
                    raise ValueError('invalid public witness observation')
                oid = row['obligation_id']
                if oid not in expected or oid in seen or type(row['observed_text']) is not str:
                    raise ValueError('invalid public witness identity')
                seen.add(oid)
                status = 'UNRESOLVED' if row['error'] is not None else ('MATCH' if row['observed_text'] == expected[oid] and (oid not in boolean_cases or row.get('observed_boolean') is True) else 'MISMATCH')
                if oid in exception_expected:
                    status = 'MATCH' if row['error'] == exception_expected[oid] else 'MISMATCH'
                if oid in typed_expected and row['error'] is None:
                    status='MATCH' if row.get('observed_typed')==typed_expected[oid] else 'MISMATCH'
                if oid in delegation_cases and row.get('delegated') is not True:
                    status = 'MISMATCH'
                if oid in boolean_expected and row['error'] is None:
                    variants = row.get('boolean_observations')
                    if (type(variants) is not list or len(variants) != 4 or any(type(v) is not dict
                            or set(v) != {'text','is_boolean'} or type(v['text']) is not str
                            or type(v['is_boolean']) is not bool for v in variants)):
                        raise ValueError('invalid Boolean truth-table observation')
                    if any(v['is_boolean'] is not True or v['text'] != expected_text
                           for v,expected_text in zip(variants,boolean_expected[oid])):
                        status = 'MISMATCH'
                for member in members[oid]:
                    missing[member] = replace(missing[member], status=status, observed_text=row['observed_text'],
                        candidate_resolution='EXPECTED_EXCEPTION' if oid in exception_expected and status=='MATCH' else 'EXECUTION_ERROR' if row['error'] is not None else 'RESOLVED',
                        detail='Expected value is a pre-mutation MODEL_HYPOTHESIS; match alone is not semantic acceptance')
        except (ValueError, TypeError, KeyError) as exc:
            return tuple(PublicWitnessObservationV1(o.obligation_id, 'UNRESOLVED', detail=str(exc)[:512]) for o in obligations)
        # A null witness does not invent a new call. It may reuse results for
        # its unique frozen origin reached through existing graph references.
        # An explicitly unresolved interpretation or invalid proposed call
        # cannot be rescued by a different witness.
        for interpretation in frozen.public_interpretations:
            if interpretation.unresolved or interpretation.witness is not None:
                continue
            obligation = next((o for o in obligations if o.obligation_id == interpretation.obligation_id), None)
            if obligation is None:
                continue
            binding = public_witness_binding(frozen, obligation)
            if binding is None:
                continue
            supporting = [missing[i.obligation_id] for i in frozen.public_interpretations
                if i.witness is not None and not i.unresolved and i.obligation_id in missing
                and (missing[i.obligation_id].origin_path,missing[i.obligation_id].origin_symbol,
                     missing[i.obligation_id].origin_signature_fingerprint) ==
                    (binding['module_path'],binding['callable_name'],binding['signature_fingerprint'])]
            if supporting:
                source = min(supporting, key=lambda r: {'MISMATCH':0,'UNRESOLVED':1,'MATCH':2}[r.status])
                missing[interpretation.obligation_id] = replace(source,
                    obligation_id=interpretation.obligation_id,evidence_id=binding['evidence_id'],
                    binding_source=binding['binding_source'],model_proposed_callable='',
                    detail='Shared frozen-origin witness from '+source.obligation_id+'; '+source.detail)
        return tuple(missing.values())
