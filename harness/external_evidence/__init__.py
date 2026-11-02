'external evidence external research and evidence public API.'
from .controller import ResearchController
from .evidence import ClaimGraph, normalize_evidence
from .models import (
    ClaimEvidenceRefV1,
    ClaimImportance,
    EvidenceItemV1,
    EvidenceRelation,
    FetchedSourceV1,
    ResearchWorkflowPolicy,
    ResearchChannel,
    ResearchClaimV1,
    ResearchCoverageV1,
    ResearchOutcome,
    ResearchQueryV1,
    ResearchReportV1,
    ResearchRequestV1,
    SearchHitV1,
    SourceKind,
    SourceTier,
    SynthesisProposalV1,
)
from .mission_bridge import MissionResearchBridge, ResearchMissionObservationV1
from .providers import GitHubRestProvider, HttpReadOnlyClient, ResearchProviderError, SearxNgProvider
from .reasoning import OrnithResearchReasoner, ResearchReasoningProtocolError
from .security import ResearchNetworkPolicyError, canonicalize_url, validate_external_url
from .service import AutonomousResearchService, build_default_research_service
from .store import JsonResearchStore

__all__ = [
    "AutonomousResearchService",
    "ClaimEvidenceRefV1", "ClaimGraph", "ClaimImportance", "EvidenceItemV1",
    "EvidenceRelation", "FetchedSourceV1", "GitHubRestProvider", "HttpReadOnlyClient",
    "JsonResearchStore", "OrnithResearchReasoner", "MissionResearchBridge", "ResearchWorkflowPolicy", "ResearchChannel",
    "ResearchClaimV1", "ResearchController", "ResearchCoverageV1", "ResearchMissionObservationV1",
    "ResearchNetworkPolicyError", "ResearchOutcome", "ResearchProviderError", "ResearchQueryV1",
    "ResearchReasoningProtocolError",
    "ResearchReportV1", "ResearchRequestV1", "SearchHitV1", "SearxNgProvider", "SourceKind",
    "SourceTier", "SynthesisProposalV1", "canonicalize_url", "normalize_evidence",
    "build_default_research_service", "validate_external_url",
]

# Explicit version-one public compatibility; not canonical exports.
def __getattr__(name):
    from harness.compatibility.public_exports import resolve_legacy_export
    return resolve_legacy_export(__name__, name)
