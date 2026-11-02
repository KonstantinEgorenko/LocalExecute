from __future__ import annotations

import os
from pathlib import Path

from harness.external_evidence.controller import ResearchController
from harness.external_evidence.models import ResearchWorkflowPolicy, ResearchChannel
from harness.external_evidence.mission_bridge import MissionResearchBridge
from harness.external_evidence.providers import GitHubRestProvider, ResearchProviderError
from harness.external_evidence.service import AutonomousResearchService
from harness.external_evidence.store import JsonResearchStore
from harness.research_reasoning import ResearchReasoningPolicy
from harness.research_contracts.policy import ResearchSemanticPolicy
from harness.research_contracts.runtime import ContractReliableOrnithResearchReasoner
from harness.runtime_hardening.github_guard import BoundedGitHubProvider
from harness.runtime_hardening.runtime_v4 import install_v4_cpu_watchdog
from harness.runtime_hardening.transport import install_runtime_hardening

from .github_runtime import IntegrityGitHubProvider
from .models import TargetEntityV1
from .policy import EvidenceIntegrityPolicy
from .web_provider import IntegrityWebProvider


def build_integrity_research_service(
    root: Path,
    *,
    state_root: Path | None = None,
    target_entity: TargetEntityV1 | None = None,
) -> AutonomousResearchService:
    root = Path(root)
    install_runtime_hardening(root)
    install_v4_cpu_watchdog(root)
    p18 = ResearchWorkflowPolicy.from_project_root(root)
    p20q = EvidenceIntegrityPolicy.from_project_root(root)
    transport = ResearchReasoningPolicy.from_project_root(root)
    semantic = ResearchSemanticPolicy.from_project_root(root)
    reasoner = ContractReliableOrnithResearchReasoner(root, p18, transport_policy=transport, semantic_policy=semantic)
    store = JsonResearchStore(state_root or (root / "state/research"))
    discovery = {}
    fetchers = {}

    searx_url = os.getenv(p18.searxng_base_url_env, "").strip()
    if p18.searxng_enabled and searx_url:
        web = IntegrityWebProvider(root, p18, p20q, base_url=searx_url)
        discovery[ResearchChannel.WEB.value] = web
        fetchers[web.provider_id] = web
    if p18.github_enabled:
        base_github = GitHubRestProvider(p18)
        frozen_github = BoundedGitHubProvider(base_github)
        github = IntegrityGitHubProvider(
            frozen_github,
            target_repository=target_entity.github_full_name if target_entity is not None else "",
            code_search_authenticated=bool(base_github.token),
        )
        discovery[ResearchChannel.GITHUB_REPOSITORIES.value] = github
        discovery[ResearchChannel.GITHUB_CODE.value] = github
        discovery[ResearchChannel.GITHUB_ISSUES.value] = github
        fetchers[github.provider_id] = github
    if not discovery:
        raise ResearchProviderError(f"no research providers configured; set {p18.searxng_base_url_env} or enable GitHub")
    controller = ResearchController(policy=p18, discovery=discovery, fetchers=fetchers, synthesis=reasoner, store=store)
    return AutonomousResearchService(reasoner=reasoner, bridge=MissionResearchBridge(controller, store))
