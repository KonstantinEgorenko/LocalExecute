'Trusted verification command payload for the opt-in integrated contract.\n\nNo C-specific filenames, formulas or oracle implementation live here.\n'
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile


def sources(root, paths):
    result={}
    for path in paths:
        target=root/path
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('GLOBAL_PATH_INVALID: '+path)
        if target.stat().st_size>65536:raise ValueError('GLOBAL_SOURCE_BOUND: '+path)
        result[path]=target.read_text(encoding='utf8')
    return result


def structure(root, contract):
    from harness.verification.test_contract import inspect_tests, functions, assertions
    paths=contract['required_source_paths']+contract['required_test_paths']
    current=sources(root,paths)
    for path,text in current.items():compile(text,path,'exec',dont_inherit=True)
    baseline={p:r['text'] for p,r in contract['baseline_tests'].items()}
    changes=inspect_tests(baseline,{p:current[p] for p in baseline},{'equivalent_function_renames':{},'deterministic_callables':[]})
    if any(changes.values()):raise ValueError('GLOBAL_BASELINE_PRESERVATION: '+json.dumps(changes))
    for path in contract['required_test_paths']:
        old=sum(sum(assertions(f).values()) for f in functions(baseline[path]).values())
        new=sum(sum(assertions(f).values()) for f in functions(current[path]).values())
        if new<=old:raise ValueError('GLOBAL_REGRESSION_ADDITION_MISSING: '+path)
    return {'syntax_paths':paths,'baseline_paths':list(baseline),'regression_paths':contract['required_test_paths']}


def semantics(root, contract, authority):
    from harness.verification.callable_authority import observe_interface
    from harness.verification.public_reference import evaluate_public_reference_value,PublicReferenceExceptionV1
    modules={};spies={};rows=[]
    sys.path.insert(0,str(root))
    declarations={(c['logical_todo_id'],c['qualified_name']):c for c in authority['callables']}
    # Dependencies are declared by the caller, not guessed from source spelling.
    def load(path):
        if path in modules:return modules[path]
        if '/' in path or not Path(path).stem.isidentifier():raise ValueError('unsupported flat public module')
        name=Path(path).stem
        spec=importlib.util.spec_from_file_location(name,root/path)
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module
        spec.loader.exec_module(module);modules[path]=module
        return module
    for declaration in authority['callables']:
        source=sources(root,[declaration['module_path']])[declaration['module_path']]
        actual=observe_interface(source,declaration['module_path'],declaration['qualified_name'],declaration['kind'],candidate=True)
        from harness.verification.callable_authority import digest
        if not actual.present or actual.signature_fingerprint!=digest(declaration['signature']):
            raise ValueError('GLOBAL_PUBLIC_SIGNATURE_MISMATCH')
        dep=declaration['delegation']
        if dep is not None:
            key=(dep['module_path'],dep['qualified_name'])
            if key not in spies:
                module=load(dep['module_path']);original=getattr(module,dep['qualified_name'])
                state={'calls':0,'probe':False}
                def traced(*args,_state=state,_original=original,**kwargs):
                    _state['calls']+=1
                    return 1000003 if _state['probe'] else _original(*args,**kwargs)
                setattr(module,dep['qualified_name'],traced);spies[key]=state
    for case in contract['semantic_cases']:
        declaration=declarations[(case['logical_todo_id'],case['qualified_name'])]
        module=load(declaration['module_path']);parts=declaration['qualified_name'].split('.')
        target=getattr(module,parts[0])
        if len(parts)==2:target=getattr(target(),parts[1])
        expected=evaluate_public_reference_value(declaration['reference_expression'],case['arguments'])
        dep=declaration['delegation'];state=spies[(dep['module_path'],dep['qualified_name'])] if dep else None
        if state:state.update(calls=0,probe=False)
        try:observed=target(**case['arguments'])
        except Exception as exc:
            if not isinstance(expected,PublicReferenceExceptionV1) or type(exc).__name__!=expected.exception_class:raise
            rows.append({'callable':case['qualified_name'],'exception':type(exc).__name__,'arguments':case['arguments']});continue
        if isinstance(expected,PublicReferenceExceptionV1) or type(observed) is not type(expected) or observed!=expected:
            raise ValueError('GLOBAL_PUBLIC_SEMANTIC_MISMATCH: '+case['qualified_name'])
        if state:
            if not state['calls']:raise ValueError('GLOBAL_DEPENDENCY_NOT_CONSUMED')
            state.update(calls=0,probe=True)
            try:probe=target(**case['arguments'])
            finally:state['probe']=False
            if not state['calls'] or type(probe) is not int or probe!=1000003:raise ValueError('GLOBAL_DEPENDENCY_RESULT_NOT_CONSUMED')
        rows.append({'callable':case['qualified_name'],'arguments':case['arguments'],'observed':observed,'delegated':bool(state)})
    return {'cases':rows,'independent_reference':'C1_2_DECLARED_PUBLIC_AUTHORITY','full_independent_oracle':'NOT_QUALIFIED'}


def pytest_run(root, contract, baseline_only):
    import pytest
    from harness.verification.test_contract import functions
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    original=Path.cwd();shadow=None
    if baseline_only:
        shadow=tempfile.TemporaryDirectory(prefix='swarm-global-baseline-');test_root=Path(shadow.name)
        files=sources(root,contract['required_source_paths'])
        files.update({p:r['text'] for p,r in contract['baseline_tests'].items()})
        for path,text in files.items():
            p=test_root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf8')
    else:test_root=root
    collected=[];executed=[]
    configuration=tempfile.TemporaryDirectory(prefix='swarm-global-pytest-config-')
    config=Path(configuration.name)/'pytest.ini';config.write_text('[pytest]\n',encoding='utf8')
    class Accounting:
        def pytest_collection_finish(self,session):
            if len(session.items)>512:pytest.exit('GLOBAL_TEST_COUNT_BOUND',returncode=2)
            collected.extend(item.nodeid for item in session.items)
        def pytest_runtest_logreport(self,report):
            if report.when=='call':executed.append({'nodeid':report.nodeid,'outcome':report.outcome})
    try:
        os.chdir(test_root);sys.path.insert(0,str(test_root))
        code=int(pytest.main(['-q','-p','no:cacheprovider','--noconftest','-c',str(config),
            '--confcutdir='+str(test_root),'--rootdir='+str(test_root),*contract['required_test_paths']],plugins=[Accounting()]))
        required=[]
        for path,row in contract['baseline_tests'].items():
            required.extend((path,name) for name in functions(row['text']))
        passed={(r['nodeid'].split('::')[0],r['nodeid'].partition('::')[2].split('[',1)[0].replace('::','.'))
                for r in executed if r['outcome']=='passed'}
        if code or any(pair not in passed for pair in required):raise ValueError('GLOBAL_PYTEST_FAILED_OR_BASELINE_NOT_EXECUTED: '+str(code))
        if not baseline_only and any(not any(r['nodeid'].split('::')[0]==p for r in executed) for p in contract['required_test_paths']):
            raise ValueError('GLOBAL_TEST_SUITE_NOT_EXECUTED')
        if not executed or any(r['outcome']!='passed' for r in executed):raise ValueError('GLOBAL_TEST_NOT_PASSED')
        return {'exit_code':code,'collected':collected,'executed':executed,'trusted_baseline_only':baseline_only}
    finally:
        os.chdir(original)
        configuration.cleanup()
        if shadow:shadow.cleanup()


def main(packet):
    mode=packet['gate'];root=Path.cwd()
    def forbid_external_effect(event,args):
        if event=='socket.connect' or event in ('subprocess.Popen','os.system','os.posix_spawn','os.exec'):
            raise RuntimeError('GLOBAL_EXTERNAL_EFFECT_FORBIDDEN: '+event)
    sys.addaudithook(forbid_external_effect)
    row={'attempt_id':packet['attempt_id'],'input_fingerprint':packet['input_fingerprint'],
        'pid':os.getpid(),'parent_pid':os.getppid(),'cwd':str(root),'gate':mode}
    # Candidate/test chatter cannot become the process identity envelope.
    with tempfile.TemporaryFile(mode='w+',encoding='utf8') as stdout, tempfile.TemporaryFile(mode='w+',encoding='utf8') as stderr:
        try:
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                if mode=='global_structure':detail=structure(root,packet['contract'])
                elif mode=='global_semantics':detail=semantics(root,packet['contract'],packet['authority'])
                elif mode in ('global_pytest','global_baseline'):detail=pytest_run(root,packet['contract'],mode=='global_baseline')
                else:raise ValueError('unknown trusted gate')
            row.update(status='PASS',detail=detail);exit_code=0
        except Exception as exc:
            row.update(status='FAIL',error_type=type(exc).__name__,error=str(exc)[:2048]);exit_code=1
        for label,stream in (('stdout',stdout),('stderr',stderr)):
            stream.flush();raw=stream.buffer;raw.seek(0);digest=hashlib.sha256();tail=b'';size=0
            while chunk:=raw.read(65536):
                digest.update(chunk);size+=len(chunk);tail=(tail+chunk)[-16384:]
            row[label+'_sha256']=digest.hexdigest();row[label+'_tail']=tail.decode('utf8',errors='replace')
            row[label+'_truncated']=size>16384
    print(json.dumps(row,sort_keys=True,allow_nan=False))
    return exit_code


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    packet=json.loads(sys.argv[1])
    sys.path.insert(0,packet['trusted_pytest_root'])
    raise SystemExit(main(packet))
