"""Version-one configuration spelling compatibility; no policy/value coercion."""
from pathlib import Path
from functools import wraps
FILENAMES = {'phase13.yaml': 'execution_policy.yaml', 'phase14.yaml': 'routing_evaluation.yaml', 'phase15.yaml': 'recovery.yaml', 'phase16.yaml': 'workflow.yaml', 'phase17.yaml': 'mission.yaml', 'phase18.yaml': 'external_evidence.yaml', 'phase18r.yaml': 'research_reasoning.yaml', 'phase18r2.yaml': 'research_contracts.yaml', 'phase19.yaml': 'research_quality.yaml', 'phase19q.yaml': 'research_remediation.yaml', 'phase20a.yaml': 'research_environment.yaml', 'phase20b.yaml': 'sandbox.yaml', 'phase20g.yaml': 'gpu_coordination.yaml', 'phase20q.yaml': 'research_integrity.yaml', 'phase20r.yaml': 'runtime_hardening.yaml', 'phase20u.yaml': 'gpu_acceleration.yaml', 'phase20v.yaml': 'model_runtime.yaml', 'phase21_2.yaml': 'production_resources.yaml'}
KEYS = {'phase14': 'routing_evaluation', 'source_phase13d_commit': 'routing_source_commit', 'max_phase16_invocations_per_run': 'max_workflow_invocations_per_run', 'require_phase16_for_execution': 'require_workflow_for_execution', 'require_frozen_phase18_parser': 'require_frozen_research_parser', 'require_phase18r_transport_boundary': 'require_reasoning_transport_boundary'}

def resolve_config_path(path):
    path = Path(path)
    semantic = path.with_name(FILENAMES.get(path.name, path.name))
    try:
        semantic.lstat()
    except FileNotFoundError:
        return path
    return semantic

def normalize_configuration_document(value):
    if isinstance(value, list):
        return [normalize_configuration_document(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        semantic = KEYS.get(key, key)
        if semantic in result:
            raise ValueError('Ambiguous legacy/current configuration key: ' + str(semantic))
        result[semantic] = normalize_configuration_document(item)
    return result

def install_legacy_fields(cls, aliases):
    original = cls.__init__
    @wraps(original)
    def initialize(self, *args, **kwargs):
        for old, new in aliases.items():
            if old in kwargs:
                if new in kwargs:
                    raise TypeError('Both legacy and semantic field supplied: ' + new)
                kwargs[new] = kwargs.pop(old)
        original(self, *args, **kwargs)
    cls.__init__ = initialize
    for old, new in aliases.items():
        setattr(cls, old, property(lambda self, name=new: getattr(self, name)))
    return cls


def legacy_keywords(aliases):
    """Decode historical call keyword spellings without changing values or policy."""
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            for old, new in aliases.items():
                if old in kwargs:
                    if new in kwargs:
                        raise TypeError('Both legacy and semantic keyword supplied: ' + new)
                    kwargs[new] = kwargs.pop(old)
            return function(*args, **kwargs)
        return invoke
    return decorate
