"""Closed import adapter; filesystem startup aliases are intentionally retired."""
import importlib
import importlib.abc
import importlib.util
import sys
from .import_mapping import MODULES, PACKAGES, ENTRYPOINTS
class LegacyLoader(importlib.abc.Loader):
    def __init__(self, name): self.name = name
    def create_module(self, spec):
        # Importlib initializes alias metadata on a fresh temporary module.
        # Returning a live canonical module here corrupts its spec during reload.
        return None
    def exec_module(self, module):
        if self.name in MODULES:
            canonical = importlib.import_module(MODULES[self.name])
            # _load_unlocked returns the final sys.modules entry after execution.
            # Never assign alias metadata to the canonical object itself.
            sys.modules[self.name] = canonical
            return
        recipe = PACKAGES[self.name]
        target = importlib.import_module(recipe['target'])
        names = getattr(target, '__all__', tuple(n for n in vars(target) if not n.startswith('_')))
        module.__dict__.update((n, getattr(target,n)) for n in names)
        if recipe['has_all']: module.__all__ = target.__all__
        for n,(source,attribute) in recipe['extras'].items(): module.__dict__[n] = getattr(importlib.import_module(source),attribute)
        if recipe['aliases']:
            def resolve(name): return getattr(target,recipe['aliases'].get(name,name))
            module.__getattr__ = resolve
    def get_filename(self, fullname):
        target = MODULES.get(fullname, PACKAGES.get(fullname, {}).get('target'))
        return importlib.util.find_spec(target).origin or '<legacy-package>'
    def get_code(self, fullname):
        if fullname not in MODULES: raise ImportError('Legacy package execution unsupported: '+fullname)
        source = ENTRYPOINTS.get(fullname, 'import importlib, sys\nsys.modules[__name__] = importlib.import_module('+repr(MODULES[fullname])+')\n')
        return compile(source,'<legacy:'+fullname+'>','exec')
    def get_source(self, fullname): return None
    def is_package(self, fullname): return fullname in PACKAGES
class LegacyImportFinder(importlib.abc.MetaPathFinder):
    _localexecute_legacy_finder = True
    def find_spec(self, fullname, path=None, target=None):
        if fullname in MODULES or fullname in PACKAGES:
            return importlib.util.spec_from_loader(fullname,LegacyLoader(fullname),is_package=fullname in PACKAGES)
        if any(fullname.startswith(root+'.') for root in PACKAGES):
            raise ModuleNotFoundError('Unknown legacy module: '+fullname,name=fullname)
        return None
def install():
    if not any(getattr(f,'_localexecute_legacy_finder',False) for f in sys.meta_path): sys.meta_path.insert(0,LegacyImportFinder())
