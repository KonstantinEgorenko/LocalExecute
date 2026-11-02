from __future__ import annotations

import json
from pathlib import Path

from harness.critic import CriticAction, CriticAnalysis, CriticPublisher, CriticReport, CriticTurnRecord, CriticTurnStatus, CriticVerdict, FailureClass
from harness.judge import JudgePolicy
from harness.lead import LeadRuntimeResponse
from harness.verification import VerificationOutcome
from tests.critic_helpers import setup_failed


def setup_failed_with_critic(tmp_path: Path):
    control, external, manager, run_store, artifacts, contract, lease, verification = setup_failed(tmp_path)
    turn = CriticTurnRecord(
        "critic-current", 0, CriticTurnStatus.RESPONSE, "ministral_reasoner", "ministral-3:14b",
        json.dumps({"raw": "critic"}), None,
    )
    publisher = CriticPublisher(run_store=run_store, artifact_store=artifacts)
    raw_id = publisher.publish_turn(turn)
    analysis = CriticAnalysis(
        VerificationOutcome.FAILED, CriticVerdict.RETRY, FailureClass.IMPLEMENTATION_ERROR, 0.95,
        "The exact required postcondition failed.", ("candidate_diff",), CriticAction.RETRY_WITH_CONTEXT,
        ("Correct the exact expected value.",), False,
    )
    report = CriticReport(
        "critic-current", "f" * 64, "r", "t", verification.report_id, verification.candidate_diff_sha256,
        analysis, "ministral_reasoner", "ministral-3:14b", 1, raw_id, "now",
    )
    publisher.publish_report(report)
    return control, external, manager, run_store, artifacts, contract, lease, verification, report


class FakeJudgeRuntime:
    def __init__(self, contents, model_id: str = "ornith_lead") -> None:
        self.contents = list(contents)
        self.model_id = model_id
        self.calls = 0
        self.requests = []

    def complete(self, system_prompt, user_prompt, response_schema=None):
        self.requests.append((system_prompt, user_prompt, response_schema))
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        return LeadRuntimeResponse(content, self.model_id, "hf.co/deepreinforce-ai/Ornith-1.0-35B-GGUF:Q4_K_M", 65536)


def valid_failed_judge_response() -> str:
    return json.dumps({
        "deterministic_outcome": "FAILED",
        "decision": "RETRY",
        "confidence": 0.94,
        "summary": "Verification failed and the critic identifies a repairable implementation error.",
        "evidence": ["verification_report", "critic_report", "candidate_diff"],
        "next_action": "RETRY_WITH_CONTEXT",
        "handoff_guidance": ["Correct the exact postcondition while preserving scope."],
        "can_override_deterministic": False,
    }, separators=(",", ":"))
