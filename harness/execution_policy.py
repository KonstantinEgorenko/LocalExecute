"""Deterministic execution depth; model scoring and acceptance remain external."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from harness.scenarios.qualified_shape import QualifiedExecutionShapeV1
from dataclasses import asdict, dataclass
from enum import StrEnum
import ast


class ExecutionModeV1(StrEnum):
    DIRECT = 'DIRECT'
    SIMPLE = 'SIMPLE'
    STANDARD = 'STANDARD'
    ADVANCED = 'ADVANCED'


DIRECT_CAPABILITY_V1 = 'DIRECT_NATIVE_SPLIT_V1'
MULTIFILE_CAPABILITY_V1 = 'STANDARD_NATIVE_MULTIFILE_SPLIT_V1'
MULTIFILE_SOURCE_LIMIT = 'NATIVE_MULTIFILE_SOURCE_LIMIT'
DIRECT_CAPABILITY_MODELS = {DIRECT_CAPABILITY_V1: 'qwen27_heavy_quality'}
DIRECT_EVIDENCE = 'logs/phase25/edit-transport-experiment-20261002.json: native split mini-set 9/9'


def isolated_computation_source(source: str) -> bool:
    'Conservative operational-risk evidence, never candidate verification.\n\n    Only pure function modules with known arithmetic imports/calls qualify.\n    Unknown effects, annotations, decorators, top-level execution fail closed.\n    This observes the original source; verification/Judge still own acceptance.\n    '
    try:
        tree=ast.parse(source)
    except SyntaxError:
        return False
    functions={n.name for n in tree.body if isinstance(n,ast.FunctionDef)}
    safe_calls={'str','int','float','bool','len','sum','range','enumerate','tuple','list',
                'min','max','round','sorted','Decimal','ValueError','TypeError'}|functions
    if functions & {'str','int','float','bool','len','sum','range','enumerate','tuple','list',
                    'min','max','round','sorted','Decimal','ValueError','TypeError'}:
        return False
    for node in tree.body:
        if isinstance(node,ast.ImportFrom):
            if node.module!='decimal' or node.level or any(n.asname or n.name not in
                    ('Decimal','ROUND_HALF_UP','ROUND_HALF_EVEN') for n in node.names):
                return False
        elif isinstance(node,ast.FunctionDef):
            if node.decorator_list or node.returns or any(a.annotation for a in
                    (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs)):
                return False
            if {a.arg for a in (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs)} & safe_calls:
                return False
            if any(not isinstance(d,ast.Constant) for d in
                   (*node.args.defaults,*(d for d in node.args.kw_defaults if d is not None))):
                return False
        elif not (isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant)
                  and isinstance(node.value.value,str)):
            return False
    for node in ast.walk(tree):
        if isinstance(node,(ast.Global,ast.Nonlocal,ast.AsyncFunctionDef,ast.ClassDef,
                            ast.With,ast.AsyncWith,ast.Await,ast.Yield,ast.YieldFrom,ast.Import)):
            return False
        if isinstance(node,ast.ImportFrom) and node not in tree.body:
            return False
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Name) and node.func.id in safe_calls:
                continue
            if (isinstance(node.func,ast.Attribute) and node.func.attr=='quantize'
                    and isinstance(node.func.value,ast.Call)
                    and isinstance(node.func.value.func,ast.Name)
                    and node.func.value.func.id=='Decimal'):
                continue
            return False
        if isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
            targets=getattr(node,'targets',(getattr(node,'target',None),))
            if any(isinstance(n,(ast.Attribute,ast.Subscript)) for t in targets for n in ast.walk(t)):
                return False
            if any(isinstance(n,ast.Name) and n.id in safe_calls for t in targets for n in ast.walk(t)):
                return False
    return bool(functions)


@dataclass(frozen=True)
class TaskAssessmentV1:
    scenario: str
    difficulty_score: int
    scope: str
    ambiguity: str
    risk: str
    target_confidence: str
    verification_confidence: str
    research_required: bool
    regression_required: bool
    source_targets: tuple[str, ...]
    test_targets: tuple[str, ...]
    previous_failure: bool = False
    declared_risk: str | None = None
    risk_evidence: str | None = None
    multifile_native: bool = False
    qualified_shape: QualifiedExecutionShapeV1 | None = None

    def __post_init__(self):
        if type(self.difficulty_score) is not int or not 1 <= self.difficulty_score <= 10:
            raise ValueError('difficulty_score must be integer 1..10')

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPolicyV1:
    mode: ExecutionModeV1
    reasons: tuple[str, ...]
    capability: str | None = None

    def to_dict(self):
        return {'mode': self.mode.value, 'reasons': list(self.reasons), 'capability': self.capability}

    @classmethod
    def select(cls, assessment):
        from harness.scenarios.qualified_shape import QualifiedExecutionShapeV1
        if (isinstance(assessment.qualified_shape, QualifiedExecutionShapeV1)
                and assessment.qualified_shape.validate_assessment(assessment)
                and assessment.multifile_native is True
                and assessment.scope == 'MULTI_FILE'
                and assessment.ambiguity == 'LOW' and assessment.risk == 'LOW'
                and not assessment.research_required and not assessment.previous_failure
                and assessment.regression_required
                and len(assessment.source_targets) == 2 and len(assessment.test_targets) == 1
                and assessment.target_confidence == assessment.verification_confidence == 'HIGH'):
            return cls(ExecutionModeV1.STANDARD, ('explicit bounded two-file SOURCE/TEST responsibility',), MULTIFILE_CAPABILITY_V1)
        if assessment.multifile_native or assessment.qualified_shape is not None:
            return cls(ExecutionModeV1.STANDARD, ('qualified multifile proof or assessment rejected',))
        checks = {
            'unsupported_scenario': assessment.scenario != 'BUGFIX_WITH_REGRESSION',
            'nonlocal_scope': assessment.scope not in ('LOCAL', 'SINGLE_FILE'),
            'ambiguity': assessment.ambiguity != 'LOW',
            'risk': assessment.risk != 'LOW',
            'research_required': assessment.research_required,
            'unknown_source_target': assessment.target_confidence != 'HIGH' or len(assessment.source_targets) != 1,
            'unknown_test_target': len(assessment.test_targets) != 1,
            'verification_unavailable': assessment.verification_confidence != 'HIGH',
            'regression_contract_missing': not assessment.regression_required,
            'previous_failure': assessment.previous_failure,
        }
        reasons = tuple(key for key, denied in checks.items() if denied)
        if not reasons:
            return cls(ExecutionModeV1.DIRECT, ('bounded Python source/test responsibility', DIRECT_EVIDENCE), DIRECT_CAPABILITY_V1)
        mode = (ExecutionModeV1.ADVANCED if assessment.research_required or assessment.risk == 'HIGH'
                else ExecutionModeV1.STANDARD if assessment.scope not in ('LOCAL', 'SINGLE_FILE')
                else ExecutionModeV1.SIMPLE)
        return cls(mode, reasons)


@dataclass(frozen=True)
class EscalationSummaryV1:
    objective: str
    initial_assessment: dict
    source_targets: tuple[str, ...]
    test_targets: tuple[str, ...]
    changed_paths: tuple[str, ...]
    current_diff: str
    source_stage_result: dict | None
    test_stage_result: dict | None
    last_verification: dict | None
    typed_failure: dict
    next_mode: str

    def to_dict(self):
        return asdict(self)
