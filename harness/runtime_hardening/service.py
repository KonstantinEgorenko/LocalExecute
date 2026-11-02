'runtime hardening additive composition over frozen research contracts contracts.'
from __future__ import annotations
import os
from pathlib import Path
from harness.external_evidence.controller import ResearchController
from harness.external_evidence.models import ResearchWorkflowPolicy,ResearchChannel
from harness.external_evidence.mission_bridge import MissionResearchBridge
from harness.external_evidence.providers import GitHubRestProvider,ResearchProviderError,SearxNgProvider
from harness.external_evidence.service import AutonomousResearchService
from harness.external_evidence.store import JsonResearchStore
from harness.research_reasoning import ResearchReasoningPolicy
from harness.research_contracts.policy import ResearchSemanticPolicy
from harness.research_contracts.runtime import ContractReliableOrnithResearchReasoner
from .github_guard import BoundedGitHubProvider
from .runtime_v4 import install_v4_cpu_watchdog
from .transport import install_runtime_hardening

def build_hardened_research_service(root:Path,*,state_root:Path|None=None)->AutonomousResearchService:
 root=Path(root); install_runtime_hardening(root); install_v4_cpu_watchdog(root)
 policy=ResearchWorkflowPolicy.from_project_root(root); transport=ResearchReasoningPolicy.from_project_root(root); semantic=ResearchSemanticPolicy.from_project_root(root)
 reasoner=ContractReliableOrnithResearchReasoner(root,policy,transport_policy=transport,semantic_policy=semantic)
 store=JsonResearchStore(state_root or (root/'state/research')); discovery={}; fetchers={}
 searx_url=os.getenv(policy.searxng_base_url_env,'').strip()
 if policy.searxng_enabled and searx_url:
  searx=SearxNgProvider(policy,base_url=searx_url); discovery[ResearchChannel.WEB.value]=searx; fetchers[searx.provider_id]=searx
 if policy.github_enabled:
  github=BoundedGitHubProvider(GitHubRestProvider(policy))
  discovery[ResearchChannel.GITHUB_REPOSITORIES.value]=github; discovery[ResearchChannel.GITHUB_CODE.value]=github; discovery[ResearchChannel.GITHUB_ISSUES.value]=github; fetchers[github.provider_id]=github
 if not discovery:raise ResearchProviderError(f'no Phase 18 providers configured; set {policy.searxng_base_url_env} or enable GitHub')
 controller=ResearchController(policy=policy,discovery=discovery,fetchers=fetchers,synthesis=reasoner,store=store)
 return AutonomousResearchService(reasoner=reasoner,bridge=MissionResearchBridge(controller,store))
