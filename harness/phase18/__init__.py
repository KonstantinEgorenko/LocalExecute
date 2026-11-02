"""Deprecated package compatibility; implementation uses semantic names."""
from harness.external_evidence import *
from harness.external_evidence import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.external_evidence")
    aliases = {'Phase18Policy': 'ResearchWorkflowPolicy', 'Phase17ResearchBridge': 'MissionResearchBridge'}
    return getattr(current, aliases.get(name, name))
