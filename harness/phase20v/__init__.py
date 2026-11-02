"""Deprecated package compatibility; implementation uses semantic names."""
from harness.model_runtime import *
from harness.model_runtime import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.model_runtime")
    aliases = {'Phase20VPolicy': 'ModelLifecyclePolicy'}
    return getattr(current, aliases.get(name, name))
