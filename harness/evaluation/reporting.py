'Stable JSON/CSV/Markdown exports for evaluation benchmark review.'
from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path

from .models import BenchmarkRun, BenchmarkSummary


def render_summary_markdown(summary: BenchmarkSummary) -> str:
    overall = summary.overall
    regret = summary.router_regret
    rows = [
        "# SwarmAgents Benchmark Summary",
        "",
        f"- Run: `{summary.run_id}`",
        f"- Summary: `{summary.summary_id}`",
        f"- Samples: **{overall.sample_count}**",
        f"- Verified success: **{overall.verified_success.rate}** ({overall.verified_success.numerator}/{overall.verified_success.denominator})",
        f"- First-pass success: **{overall.first_pass_success.rate}**",
        f"- Latency mean / p50 / p95: **{overall.latency.mean_ms} / {overall.latency.p50_ms} / {overall.latency.p95_ms} ms**",
        "",
        "## Worker metrics",
        "",
        "| Model | N | Verified | First-pass | Mean ms | P95 ms | Model calls | Tool calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, metric in summary.by_model.items():
        rows.append(
            f"| {model} | {metric.sample_count} | {metric.verified_success.rate} | {metric.first_pass_success.rate} | "
            f"{metric.latency.mean_ms} | {metric.latency.p95_ms} | {metric.mean_model_calls} | {metric.mean_tool_calls} |"
        )
    rows.extend([
        "",
        "## Router regret",
        "",
        f"- Comparable groups: **{regret.comparable_groups}**",
        f"- Oracle-success groups: **{regret.oracle_success_groups}**",
        f"- Avoidable failures: **{regret.avoidable_failures}**",
        f"- Avoidable failure rate: **{regret.avoidable_failure_rate}**",
        f"- Router-oracle win rate: **{regret.router_oracle_win_rate}**",
        f"- Mean latency regret: **{regret.mean_latency_regret_ms} ms**",
        "",
    ])
    return "\n".join(rows)


def run_csv(run: BenchmarkRun) -> str:
    output = StringIO()
    fields = [
        "run_id", "case_id", "task_class", "difficulty", "repetition", "model_id", "router_selected",
        "verified_success", "verification_outcome", "final_status", "latency_ms", "model_calls", "tool_calls",
        "prompt_tokens", "output_tokens", "retry_count", "escalation_count", "protocol_failures",
        "critic_failure_class", "judge_decision", "error_category",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in sorted(run.records, key=lambda r: (r.case_id.casefold(), r.repetition, r.model_id.casefold())):
        measurement = record.measurement
        writer.writerow({
            "run_id": run.run_id,
            "case_id": record.case_id,
            "task_class": record.task_class.value,
            "difficulty": record.difficulty.value,
            "repetition": record.repetition,
            "model_id": record.model_id,
            "router_selected": record.router_selected,
            "verified_success": measurement.verified_success,
            "verification_outcome": measurement.verification_outcome.value,
            "final_status": measurement.final_status.value,
            "latency_ms": measurement.latency_ms,
            "model_calls": measurement.model_calls,
            "tool_calls": measurement.tool_calls,
            "prompt_tokens": measurement.prompt_tokens,
            "output_tokens": measurement.output_tokens,
            "retry_count": measurement.retry_count,
            "escalation_count": measurement.escalation_count,
            "protocol_failures": measurement.protocol_failures,
            "critic_failure_class": measurement.critic_failure_class.value,
            "judge_decision": measurement.judge_decision.value if measurement.judge_decision else "",
            "error_category": measurement.error_category or "",
        })
    return output.getvalue()


def write_exports(*, run: BenchmarkRun, summary: BenchmarkSummary, output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run.run_id}.summary.json"
    csv_path = output_dir / f"{run.run_id}.records.csv"
    md_path = output_dir / f"{run.run_id}.summary.md"
    json_path.write_text(json.dumps(summary.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    csv_path.write_text(run_csv(run), encoding="utf-8", newline="\n")
    md_path.write_text(render_summary_markdown(summary), encoding="utf-8", newline="\n")
    return json_path, csv_path, md_path
