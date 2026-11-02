"""Deprecated package compatibility; implementation uses semantic names."""
from harness.research_integrity import *
from harness.research_integrity import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.research_integrity")
    aliases = {'Phase20QPolicy': 'EvidenceIntegrityPolicy', 'Phase20QClausewiseEntailmentJudge': 'IntegrityClausewiseEntailmentJudge', 'Phase20QTargetedResearchRunner': 'IntegrityTargetedResearchRunner', 'Phase20QRemediationContext': 'IntegrityRemediationContext'}
    return getattr(current, aliases.get(name, name))
