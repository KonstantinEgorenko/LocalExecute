'runtime hardening additive research-runtime hardening policy.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RuntimeHardeningPolicy:
    max_selected_sources: int
    max_selected_evidence: int
    max_excerpt_chars_per_evidence: int
    min_excerpt_chars_per_evidence: int
    max_synthesis_user_utf8_bytes: int
    max_retry_previous_response_chars: int
    preserve_primary_first: bool
    preserve_source_diversity: bool
    preserve_topic_coverage: bool
    require_cpu_execution: bool
    expected_num_gpu: int
    disable_thinking_for_json_contracts: bool
    lead_request_timeout_seconds: int
    lead_base_url: str
    cpu_probe_timeout_seconds: int
    research_timeout_seconds: int
    remediation_timeout_seconds: int

    @classmethod
    def from_project_root(cls, root: Path) -> "RuntimeHardeningPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase20r.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        data = raw["research_runtime_hardening_v1"]
        synthesis = data["synthesis"]
        lead = data["lead"]
        live = data["live_acceptance"]
        policy = cls(
            max_selected_sources=int(synthesis["max_selected_sources"]),
            max_selected_evidence=int(synthesis["max_selected_evidence"]),
            max_excerpt_chars_per_evidence=int(
                synthesis["max_excerpt_chars_per_evidence"]
            ),
            min_excerpt_chars_per_evidence=int(
                synthesis["min_excerpt_chars_per_evidence"]
            ),
            max_synthesis_user_utf8_bytes=int(
                synthesis["max_synthesis_user_utf8_bytes"]
            ),
            max_retry_previous_response_chars=int(
                synthesis["max_retry_previous_response_chars"]
            ),
            preserve_primary_first=bool(synthesis["preserve_primary_first"]),
            preserve_source_diversity=bool(
                synthesis["preserve_source_diversity"]
            ),
            preserve_topic_coverage=bool(synthesis["preserve_topic_coverage"]),
            require_cpu_execution=bool(lead["require_cpu_execution"]),
            expected_num_gpu=int(lead["expected_num_gpu"]),
            disable_thinking_for_json_contracts=bool(
                lead["disable_thinking_for_json_contracts"]
            ),
            lead_request_timeout_seconds=int(lead["request_timeout_seconds"]),
            lead_base_url=str(live["lead_base_url"]).rstrip("/"),
            cpu_probe_timeout_seconds=int(live["cpu_probe_timeout_seconds"]),
            research_timeout_seconds=int(live["research_timeout_seconds"]),
            remediation_timeout_seconds=int(live["remediation_timeout_seconds"]),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.max_selected_sources <= 0:
            raise ValueError("max_selected_sources must be positive")
        if self.max_selected_evidence <= 0:
            raise ValueError("max_selected_evidence must be positive")
        if self.max_excerpt_chars_per_evidence <= 0:
            raise ValueError("max_excerpt_chars_per_evidence must be positive")
        if not (
            0
            < self.min_excerpt_chars_per_evidence
            <= self.max_excerpt_chars_per_evidence
        ):
            raise ValueError("invalid minimum excerpt limit")
        if self.max_synthesis_user_utf8_bytes < 8192:
            raise ValueError("synthesis byte budget is implausibly small")
        if not (
            0
            < self.max_retry_previous_response_chars
            < self.max_synthesis_user_utf8_bytes
        ):
            raise ValueError("invalid retry previous-response budget")
        if self.expected_num_gpu != 0:
            raise ValueError(
                "Phase20R CPU Lead contract requires expected_num_gpu=0"
            )
        if self.lead_request_timeout_seconds < 180:
            raise ValueError(
                "CPU Lead request timeout must not be below frozen runtime timeout"
            )
        if self.cpu_probe_timeout_seconds < self.lead_request_timeout_seconds:
            raise ValueError(
                "CPU probe timeout must cover one hardened Lead request"
            )
        if self.research_timeout_seconds < self.lead_request_timeout_seconds:
            raise ValueError(
                "research timeout must cover one hardened Lead request"
            )
        if self.remediation_timeout_seconds < self.research_timeout_seconds:
            raise ValueError(
                "remediation timeout must cover targeted research"
            )
        if not self.lead_base_url.startswith("http://127.0.0.1:"):
            raise ValueError(
                "Phase20R live lead endpoint must remain loopback"
            )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20RPolicy = RuntimeHardeningPolicy
