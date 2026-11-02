import json
import pytest

from harness.judge import JudgeDecision, JudgeProtocolError, parse_judge_response
from tests.judge_helpers import setup_failed_with_critic, valid_failed_judge_response
from harness.judge import JudgeEvidenceResolver, JudgePolicy


def bundle(tmp_path):
    _, _, manager, runs, artifacts, contract, lease, _, _ = setup_failed_with_critic(tmp_path)
    return JudgeEvidenceResolver(run_store=runs, artifact_store=artifacts, manager=manager, policy=JudgePolicy()).resolve(run_id="r", contract=contract, lease_id=lease.lease_id)


def test_valid_failed_decision(tmp_path):
    result = parse_judge_response(valid_failed_judge_response(), bundle(tmp_path))
    assert result.decision is JudgeDecision.RETRY


def test_failed_accept_is_rejected(tmp_path):
    payload = json.loads(valid_failed_judge_response())
    payload["decision"] = "ACCEPT"; payload["next_action"] = "NONE"
    with pytest.raises(JudgeProtocolError, match="cannot ACCEPT"):
        parse_judge_response(json.dumps(payload), bundle(tmp_path))


def test_requires_verification_and_critic_grounding(tmp_path):
    payload = json.loads(valid_failed_judge_response())
    payload["evidence"] = ["critic_report", "candidate_diff"]
    with pytest.raises(JudgeProtocolError, match="must cite"):
        parse_judge_response(json.dumps(payload), bundle(tmp_path))


def test_unknown_evidence_is_rejected(tmp_path):
    payload = json.loads(valid_failed_judge_response())
    payload["evidence"] = ["verification_report", "critic_report", "made_up"]
    with pytest.raises(JudgeProtocolError, match="unknown evidence"):
        parse_judge_response(json.dumps(payload), bundle(tmp_path))
