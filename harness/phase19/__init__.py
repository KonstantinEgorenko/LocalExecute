"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_quality import *
from harness.research_quality import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_quality")
    aliases = {'Phase19Policy': 'ResearchQualityPolicy'}
    return getattr(current, aliases.get(name, name))
