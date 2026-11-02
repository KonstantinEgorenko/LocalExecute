"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_remediation import *
from harness.research_remediation import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_remediation")
    aliases = {'Phase19QPolicy': 'ResearchRemediationPolicy'}
    return getattr(current, aliases.get(name, name))
