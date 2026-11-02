'Typed bridge from frozen pipeline results into evaluation measurements.'
from __future__ import annotations

from typing import Iterable

from ..agent_loop.models import AgentLoopResult
from ..critic.models import CriticReport, FailureClass
from ..executor.models import WorkerResult
from ..judge.models import JudgeReport
from ..scheduler.models import SchedulerTodoState
from ..verification.models import VerificationReport
from .models import BenchmarkMeasurement


def _ollama_ns_to_ms(value: int | None) -> int:
    if value is None or value <= 0:
        return 0
    return int(round(value / 1_000_000))


class PipelineMeasurementBuilder:
    """No inference: project already-frozen pipeline facts into benchmark metrics."""

    @staticmethod
    def build(
        *,
        agent_result: AgentLoopResult,
        worker_results: Iterable[WorkerResult],
        verification_report: VerificationReport,
        scheduler_state: SchedulerTodoState,
        latency_ms: int,
        critic_report: CriticReport | None = None,
        judge_report: JudgeReport | None = None,
        retry_count: int | None = None,
        escalation_count: int = 0,
        protocol_failures: int = 0,
        peak_vram_mb: int | None = None,
        peak_ram_mb: int | None = None,
        error_category: str | None = None,
    ) -> BenchmarkMeasurement:
        workers = tuple(worker_results)
        if verification_report.run_id != agent_result.run_id or verification_report.todo_id != agent_result.todo_id:
            raise ValueError("verification report is not bound to agent result")
        if scheduler_state.run_id != agent_result.run_id or scheduler_state.todo_id != agent_result.todo_id:
            raise ValueError("scheduler state is not bound to agent result")
        if critic_report is not None:
            if critic_report.run_id != agent_result.run_id or critic_report.todo_id != agent_result.todo_id:
                raise ValueError("critic report is not bound to agent result")
            if critic_report.verification_report_id != verification_report.report_id:
                raise ValueError("critic report is not bound to verification report")
        if judge_report is not None:
            if judge_report.run_id != agent_result.run_id or judge_report.todo_id != agent_result.todo_id:
                raise ValueError("judge report is not bound to agent result")
            if judge_report.verification_report_id != verification_report.report_id:
                raise ValueError("judge report is not bound to verification report")
            if critic_report is None or judge_report.critic_report_id != critic_report.report_id:
                raise ValueError("judge report requires matching critic report")
        retries = max(0, scheduler_state.attempt_count - 1) if retry_count is None else retry_count
        return BenchmarkMeasurement(
            verification_outcome=verification_report.outcome,
            final_status=scheduler_state.status,
            latency_ms=latency_ms,
            model_calls=len(workers),
            tool_calls=agent_result.tool_calls,
            prompt_tokens=sum(result.prompt_eval_count or 0 for result in workers),
            output_tokens=sum(result.eval_count or 0 for result in workers),
            retry_count=retries,
            escalation_count=escalation_count,
            protocol_failures=protocol_failures,
            load_duration_ms=sum(_ollama_ns_to_ms(result.load_duration) for result in workers),
            generation_duration_ms=sum(_ollama_ns_to_ms(result.eval_duration) for result in workers),
            peak_vram_mb=peak_vram_mb,
            peak_ram_mb=peak_ram_mb,
            critic_failure_class=critic_report.analysis.failure_class if critic_report else FailureClass.NONE,
            judge_decision=judge_report.analysis.decision if judge_report else None,
            error_category=error_category,
            metadata={
                "agent_outcome": agent_result.outcome.value,
                "changed_paths": list(agent_result.changed_paths),
                "verification_report_id": verification_report.report_id,
                "critic_report_id": critic_report.report_id if critic_report else None,
                "judge_report_id": judge_report.report_id if judge_report else None,
            },
        )
