"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_contracts import *
from harness.research_contracts import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_contracts")
    aliases = {'Phase18R2Policy': 'ResearchSemanticPolicy'}
    return getattr(current, aliases.get(name, name))
