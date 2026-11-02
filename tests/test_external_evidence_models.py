import pytest
from dataclasses import replace

from harness.external_evidence.models import (
    ClaimEvidenceRefV1, ClaimImportance, EvidenceRelation, ResearchChannel,
    ResearchClaimV1, ResearchQueryV1, ResearchRequestV1,
)
from tests.external_evidence_helpers import policy, request


def test_policy_requires_read_only_invariants():
    p = policy()
    assert p.require_read_only_tools is True
    with pytest.raises(ValueError):
        replace(p, require_read_only_tools=False)


def test_request_requires_unique_topics():
    with pytest.raises(ValueError):
        ResearchRequestV1("r", "q", ("x", "x"), (ResearchQueryV1("q1", "x", ResearchChannel.WEB),))


def test_query_channel_is_strict():
    with pytest.raises(ValueError):
        ResearchQueryV1("q1", "x", "INVALID")


def test_claim_requires_evidence():
    with pytest.raises(ValueError):
        ResearchClaimV1("claim:1", "x", "concurrency", ClaimImportance.IMPORTANT, ())


def test_request_round_trip_shape():
    raw = request().to_dict()
    assert raw["schema_version"] == 1
    assert len(raw["initial_queries"]) == 2
