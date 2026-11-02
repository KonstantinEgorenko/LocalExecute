"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_environment import *
from harness.research_environment import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_environment")
    aliases = {'Phase20APolicy': 'ResearchEnvironmentPolicy'}
    return getattr(current, aliases.get(name, name))
