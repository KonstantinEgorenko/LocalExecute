'Explicit bounded native edit policy; verification remains acceptance authority.'
from dataclasses import dataclass
import re

SOURCE_REPAIR_POLICY = 'SOURCE_REPAIR_GENERATIONS'
TEST_REPAIR_POLICY = 'TEST_REPAIR_GENERATIONS'


class SourceExecutionFailure(RuntimeError):
    'Terminal native failure, handled by the existing mission boundary.'
    def __init__(self, message, *, failure_diagnostic=None):
        super().__init__(message)
        self.failure_diagnostic=failure_diagnostic


class TestExecutionFailure(RuntimeError):
    """Terminal TEST responsibility failure with durable verification evidence."""
    def __init__(self, message, *, failure_diagnostic=None):
        super().__init__(message)
        self.failure_diagnostic=failure_diagnostic


@dataclass(frozen=True)
class SourceRepairPolicy:
    repair_generations: int
    test_repair_generations: int | None = None

    @classmethod
    def from_constraints(cls, constraints):
        if SOURCE_REPAIR_POLICY not in constraints and TEST_REPAIR_POLICY not in constraints:
            return None  # Preserve existing callers' execution semantics.
        value = constraints.get(SOURCE_REPAIR_POLICY, 0)
        if type(value) is not int or value not in (0, 1):
            raise ValueError('SOURCE_REPAIR_GENERATIONS must be integer 0 or 1')
        test = constraints.get(TEST_REPAIR_POLICY)
        if TEST_REPAIR_POLICY in constraints and (type(test) is not int or test not in (0, 1)):
            raise ValueError('TEST_REPAIR_GENERATIONS must be integer 0 or 1')
        return cls(value, test)

    def limit(self, responsibility):
        return 1 + (self.repair_generations if responsibility == 'SOURCE' else
                    (self.test_repair_generations or 0))


def test_repairable(report):
    """Only fresh, complete deterministic TEST failures; never errors/timeouts."""
    if not report or report.get('outcome') != 'FAILED' or not report.get('report_id'):
        return False
    from harness.verification.models import VerificationReport
    try:
        VerificationReport.from_dict(report)
    except (KeyError, TypeError, ValueError):
        return False
    results = report.get('results', [])
    failed = [g for g in results if g.get('outcome') != 'PASSED']
    return bool(failed) and all(g.get('outcome') == 'FAILED' and g['gate_id'] in
        {'pytest', 'regression_preservation', 'regression_oracle'} for g in failed) and all(
        any(g['gate_id'] == name and g['outcome'] == 'PASSED' for g in results)
        for name in ('candidate_diff', 'scope_guard', 'python_syntax', 'immutable_baseline_tests'))


def repair_evidence(objective, sources, diff, report, remaining):
    tests = [g for g in report['results'] if g['gate_id'] == 'pytest']
    stdout = '\n'.join(g.get('stdout', '') for g in tests)
    return dict(original_objective=objective, current_source=sources,
        actual_diff=diff[:65536], verification_report=report,
        failed_test_names=re.findall(r'^FAILED\s+(\S+)', stdout, re.MULTILINE),
        pytest_stdout=stdout[:65536],
        pytest_stderr='\n'.join(g.get('stderr', '') for g in tests)[:65536],
        expected_actual_observations=[line for line in stdout.splitlines()
            if line.startswith(('E ', '> '))][:32], remaining_source_repair_generations=remaining)
