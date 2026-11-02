"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_reasoning import *
from harness.research_reasoning import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_reasoning")
    aliases = {'Phase18RPolicy': 'ResearchReasoningPolicy'}
    return getattr(current, aliases.get(name, name))
