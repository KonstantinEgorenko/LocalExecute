"""Fresh-process compatibility scheduling; test-only, no runtime instrumentation."""
import importlib
import importlib.abc
import importlib.machinery
import json
import sys
import threading
from harness.compatibility.imports import LegacyLoader, LegacyImportFinder

OLD = 'harness.phase17.models'
NEW = 'harness.mission.models'

def check(module):
    assert module.__name__ == NEW
    assert module.__spec__.name == NEW
    assert module.__package__ == 'harness.mission'
    assert not isinstance(module.__loader__, LegacyLoader)
    assert importlib.import_module(OLD) is module
    import harness.phase17 as parent
    assert parent.models is module
    for _ in range(3):
        assert importlib.reload(module) is module
        assert module.__spec__.name == NEW
        assert module.__package__ == 'harness.mission'
    try: importlib.import_module('harness.phase17.unknown_target')
    except ModuleNotFoundError: pass
    else: raise AssertionError('UNKNOWN_TARGET_ACCEPTED')

def concurrent():
    module = importlib.import_module(NEW)
    entered, release, chosen = threading.Event(), threading.Event(), threading.Event()
    original, finding = LegacyLoader.exec_module, LegacyImportFinder.find_spec
    errors = []
    observations = {}
    def pause(self, temporary):
        if self.name == OLD:
            observations['during_spec'] = module.__spec__.name
            observations['during_package'] = module.__package__
            entered.set()
            if not release.wait(10): raise AssertionError('SCHEDULING_TIMEOUT')
        return original(self, temporary)
    def find(self, fullname, path=None, target=None):
        result = finding(self, fullname, path, target)
        if target is module: chosen.set()
        return result
    # Canonical reload uses its SourceFileLoader directly; observe selection with
    # a test-local finder that delegates and returns None.
    class ReloadObserver(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if target is module: chosen.set()
            return None
    sys.meta_path.insert(0, ReloadObserver())
    LegacyLoader.exec_module, LegacyImportFinder.find_spec = pause, find
    def invoke(fn):
        try: fn()
        except BaseException as exc: errors.append(repr(exc))
    a = threading.Thread(target=invoke, args=(lambda: importlib.import_module(OLD),))
    b = threading.Thread(target=invoke, args=(lambda: importlib.reload(module),))
    a.start()
    try:
        assert entered.wait(5)
        b.start()
        assert chosen.wait(5)
    finally: release.set()
    a.join(10); b.join(10)
    assert not a.is_alive() and not b.is_alive(), 'DEADLOCK'
    assert not errors, errors
    assert observations == {'during_spec': NEW, 'during_package': 'harness.mission'}, observations
    LegacyLoader.exec_module, LegacyImportFinder.find_spec = original, finding
    check(module)

def initializing():
    entered, release, alias_requested = threading.Event(), threading.Event(), threading.Event()
    errors = []
    class InitializingLoader(importlib.abc.Loader):
        def __init__(self, inner): self.inner = inner
        def create_module(self, spec): return self.inner.create_module(spec)
        def exec_module(self, module):
            entered.set()
            if not release.wait(10): raise AssertionError('INIT_TIMEOUT')
            self.inner.exec_module(module)
    class Scheduler(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'harness.phase17': alias_requested.set()
            if fullname == NEW:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                spec.loader = InitializingLoader(spec.loader)
                return spec
            return None
    finder = Scheduler();sys.meta_path.insert(0, finder)
    def invoke(name):
        try: importlib.import_module(name)
        except BaseException as exc: errors.append(repr(exc))
    a = threading.Thread(target=invoke, args=(NEW,));b = threading.Thread(target=invoke, args=(OLD,))
    a.start()
    try:
        assert entered.wait(5)
        b.start()
        assert alias_requested.wait(5)
    finally: release.set()
    a.join(10);b.join(10);assert not a.is_alive() and not b.is_alive(), 'DEADLOCK'
    assert not errors, errors
    sys.meta_path.remove(finder)
    check(importlib.import_module(NEW))

def main(case):
    if case == 'concurrent': concurrent()
    elif case == 'initializing': initializing()
    elif case == 'legacy-first': check(importlib.import_module(OLD))
    elif case == 'canonical-first':
        module = importlib.import_module(NEW)
        assert importlib.import_module(OLD) is module
        check(module)
    else: raise ValueError(case)
    print(json.dumps({'case': case, 'status': 'PASS'}))

if __name__ == '__main__': main(sys.argv[1])
