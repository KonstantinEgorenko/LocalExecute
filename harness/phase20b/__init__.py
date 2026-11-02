"""Deprecated package compatibility; implementation uses semantic names."""
from harness.sandbox import *
from harness.sandbox import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.sandbox")
    aliases = {'Phase20BPolicy': 'SandboxPolicy'}
    return getattr(current, aliases.get(name, name))
