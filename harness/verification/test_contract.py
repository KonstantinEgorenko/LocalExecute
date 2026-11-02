"""Opt-in deterministic Python test contract. No candidate-derived oracle."""
import ast
from collections import Counter
from collections.abc import Mapping
import json
from pathlib import Path
import tempfile

KEY = 'DETERMINISTIC_TEST_CONTRACT'


def policy(constraints):
    value = constraints.get(KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get('version') != 1 or type(value.get('version')) is not int:
        raise ValueError('invalid deterministic test contract version')
    if value.get('preserve_baseline') is not True or value.get('differential') is not True:
        raise ValueError('deterministic test contract requires baseline preservation and differential')
    names = value.get('deterministic_callables', ())
    if not names or any(type(n) is not str or not n.isidentifier() for n in names):
        raise ValueError('explicit deterministic callable names required')
    return value


def functions(source):
    tree = ast.parse(source)
    result = {}
    def visit(body, prefix=''):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + '.')
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                result[prefix + node.name] = node
    visit(tree.body)
    return result


def dump(node):
    return ast.dump(node, include_attributes=False)


def assertions(function):
    """Keep control-flow/decorator context; a disabled assert is not coverage."""
    result=Counter()
    if function is None: return result
    decorators=tuple(dump(n) for n in function.decorator_list)
    def visit(body, context):
        bindings=[]
        for node in body:
            if isinstance(node, ast.Assert):
                result[repr((decorators,context,tuple(bindings),dump(node)))]+=1
            elif isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign,ast.Expr,ast.Return,ast.Raise)):
                if not (isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)):
                    bindings.append(dump(node))
            else:
                for field in ('body','orelse','finalbody'):
                    branch=getattr(node,field,None)
                    if isinstance(branch,list):
                        label=(type(node).__name__,field,dump(node.test) if isinstance(node,ast.If) else dump(node))
                        visit(branch,context+(label,))
    visit(function.body,())
    return result


def inspect_tests(baseline, candidate, contract):
    """Preserve assert AST multiplicity/function binding; explicit rename proof."""
    missing = []; conflicts = []
    renames = contract.get('equivalent_function_renames', {})
    for path, original in baseline.items():
        before = functions(original); after = functions(candidate.get(path, ''))
        for name, func in before.items():
            target = renames.get(path + ':' + name, name)
            current = after.get(target)
            if target != name:
                # The separate equivalence contract authorizes only exact
                # function renaming, not guessing that changed assertions agree.
                import copy
                left, right = copy.deepcopy(func), copy.deepcopy(current)
                if right is None:
                    missing.append(path + ':' + name); continue
                left.name = right.name = 'test_equivalent'
                if dump(left) != dump(right):
                    missing.append(path + ':' + name + ':UNPROVEN_REFACTOR'); continue
            old = assertions(func)
            new = assertions(current)
            missing.extend({'path':path, 'function':name, 'assertion':a, 'count':count}
                for a,count in (old-new).items())
    for path, source in candidate.items():
        for name, func in functions(source).items():
            seen = {}
            for stmt in func.body:
                # Reject only consecutive pure literal calls explicitly declared
                # deterministic. Assignment/control flow/other calls reset scope.
                expr = stmt.test if isinstance(stmt, ast.Assert) else None
                if not (isinstance(expr, ast.Compare) and len(expr.ops)==1 and isinstance(expr.ops[0], ast.Eq)
                        and isinstance(expr.comparators[0], ast.Constant)):
                    seen.clear(); continue
                call = expr.left
                inner = call
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'str' and len(call.args)==1 and not call.keywords:
                    inner = call.args[0]
                if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id in contract['deterministic_callables']):
                    seen.clear(); continue
                try:
                    for argument in inner.args: ast.literal_eval(argument)
                    for kw in inner.keywords:
                        if kw.arg is None: raise ValueError('star arguments')
                        ast.literal_eval(kw.value)
                except (ValueError, TypeError):
                    seen.clear(); continue
                key = dump(call); expected = dump(expr.comparators[0])
                if key in seen and seen[key] != expected:
                    conflicts.append({'path':path,'function':name,'call':key,'expected_constants':[seen[key],expected]})
                seen[key] = expected
    return {'missing_baseline_assertions':missing,'contradictory_assertions':conflicts}


def committed_files(runner, root, snapshot):
    """Read the immutable lease base, never the mutable candidate HEAD."""
    lease = runner.manager.get_active(snapshot.lease_id)
    base = lease.base_commit
    rows = runner.manager.git.run(root, 'ls-tree', '-r', '-z', base).stdout.split('\0')
    files = {}; total = 0
    for row in rows:
        if not row: continue
        meta, path = row.split('\t', 1); mode, kind, blob = meta.split()
        if not path.endswith('.py'): continue
        target = runner._safe_path(root, path, allow_missing=True)
        if mode not in ('100644','100755') or kind != 'blob':
            raise ValueError('linked Python baseline unsupported')
        size = int(runner.manager.git.text(root, 'cat-file', '-s', blob)); total += size
        if size > runner.policy.max_source_bytes or total > runner.policy.max_source_bytes:
            raise ValueError('bounded baseline exceeded')
        files[path] = runner.manager.git.run(root, 'cat-file', 'blob', blob).stdout
    return files


def test_files(files):
    from harness.repo_intelligence.scanner import _kind
    from harness.repo_intelligence.models import RepositoryFileKind
    return {p:s for p,s in files.items() if _kind(p) is RepositoryFileKind.TEST}


def shadow_pytest(runner, root, files, timeout):
    """Run trusted Python against bounded isolated copies; never modify candidate."""
    with tempfile.TemporaryDirectory(prefix='swarm-test-contract-') as folder:
        shadow = Path(folder)
        for path, text in files.items():
            target=shadow/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text,encoding='utf-8')
        # Reuse verification command timeout, minimal environment, bounded output and
        # process-tree cleanup. Only the copied working directory differs.
        import copy
        from dataclasses import replace
        from .models import CommandCatalogEntry, VerificationGateSpec, VerificationGateKind, VerificationGateType
        entry=CommandCatalogEntry('isolated_test_contract',VerificationGateType.TESTS,
            ('{python}','-m','pytest','-q','-p','no:cacheprovider',*sorted(test_files(files))),runner.policy.max_gate_timeout_seconds)
        isolated=copy.copy(runner)
        isolated._trusted_python_cache=runner._trusted_python_executable(root=root,env=runner._minimal_env(root=root))
        isolated.policy=replace(runner.policy,command_catalog={**runner.policy.command_catalog,entry.catalog_id:entry})
        gate=VerificationGateSpec(entry.catalog_id,VerificationGateKind.COMMAND,VerificationGateType.TESTS,True,catalog_id=entry.catalog_id)
        return isolated._command(gate,shadow,max_seconds=timeout)


def evaluate(runner, gate, contract, root, snapshot, timeout):
    from .models import GateExecutionResult, GateOutcome
    cfg=policy(contract.constraints)
    if cfg is None: raise ValueError('test contract gate without opt-in')
    baseline=committed_files(runner,root,snapshot); tests=test_files(baseline)
    if not tests: raise ValueError('immutable baseline tests required')
    current={p:runner._safe_path(root,p,allow_missing=True).read_text(encoding='utf-8') for p in baseline}
    if gate.gate_id=='regression_preservation':
        details=inspect_tests(tests,{p:current[p] for p in tests},cfg)
        ok=not any(details.values());stdout=json.dumps(details,ensure_ascii=False)
        return GateExecutionResult(gate.gate_id,gate.gate_type,True,GateOutcome.PASSED if ok else GateOutcome.FAILED,0,stdout,stdout=stdout)
    files={**current,**tests} if gate.gate_id=='immutable_baseline_tests' else {**baseline,**{p:current[p] for p in tests}}
    q=shadow_pytest(runner,root,files,timeout)
    ok=q.exit_code==0 if gate.gate_id=='immutable_baseline_tests' else q.exit_code==1 and 'AssertionError' in q.stdout and 'ERROR collecting' not in q.stdout
    outcome=q.outcome if q.outcome in (GateOutcome.ERROR,GateOutcome.TIMEOUT) else (GateOutcome.PASSED if ok else GateOutcome.FAILED)
    return GateExecutionResult(gate.gate_id,gate.gate_type,True,outcome,0,
        'immutable baseline on candidate SOURCE' if gate.gate_id=='immutable_baseline_tests' else 'candidate regression must reject original SOURCE by assertion',
        exit_code=q.exit_code,stdout=q.stdout,stderr=q.stderr,output_truncated=q.output_truncated)
