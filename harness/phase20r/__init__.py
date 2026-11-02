"""Deprecated package compatibility; implementation uses semantic names."""
from harness.runtime_hardening import *
from harness.runtime_hardening import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.runtime_hardening")
    aliases = {'Phase20RPolicy': 'RuntimeHardeningPolicy', 'Phase20RTransportError': 'RuntimeHardeningTransportError', 'Phase20RTargetedResearchRunner': 'ContextBoundedResearchRunner'}
    return getattr(current, aliases.get(name, name))
