'Enhanced WEB fetch path using frozen security and research environment browser fallback.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from pathlib import Path
from typing import Sequence

from harness.external_evidence.models import FetchedSourceV1, ResearchWorkflowPolicy, ResearchQueryV1, SearchHitV1
from harness.external_evidence.provenance import classify_source
from harness.external_evidence.providers import HttpReadOnlyClient, ResearchProviderError, SearxNgProvider
from harness.external_evidence.security import canonicalize_url

from .acquisition import AcquisitionRouter, make_envelope
from .cleaner import StructuralContentCleaner
from .policy import EvidenceIntegrityPolicy


class IntegrityWebProvider:
    """SearXNG discovery + Swarm-native deterministic fetch/clean/escalate."""

    provider_id = "searxng"

    @legacy_keywords({'phase18': 'research_policy', 'phase20q': 'integrity_policy'})
    def __init__(self, root: Path, research_policy: ResearchWorkflowPolicy, integrity_policy: EvidenceIntegrityPolicy, *, base_url: str, client: HttpReadOnlyClient | None = None) -> None:
        self.root = Path(root)
        self.external_evidence = research_policy
        self.research_integrity = integrity_policy
        self.client = client or HttpReadOnlyClient(research_policy)
        self.discovery = SearxNgProvider(research_policy, base_url=base_url, client=self.client)
        self.cleaner = StructuralContentCleaner()
        self.router = AcquisitionRouter(integrity_policy.browser_escalation_min_text_chars)

    def search(self, query: ResearchQueryV1, *, limit: int) -> Sequence[SearchHitV1]:
        return self.discovery.search(query, limit=limit)

    def _browser_fetch(self, url: str) -> tuple[str, str, str]:
        try:
            from harness.research_environment.browser import PlaywrightBrowserSession
            from harness.research_environment.policy import ResearchEnvironmentPolicy
            policy = ResearchEnvironmentPolicy.from_project_root(self.root)
            downloads = self.root / "state" / "research" / "phase20q_browser_downloads"
            with PlaywrightBrowserSession(policy, downloads_dir=downloads) as browser:
                observation = browser.navigate(url)
                return observation.text, observation.title, observation.observed_at
        except Exception as exc:
            raise ResearchProviderError(f"Phase20Q browser fallback unavailable: {exc}") from exc

    def fetch(self, hit: SearchHitV1) -> FetchedSourceV1:
        final_url, status, content_type, data = self.client._request(hit.url)
        if status != 200:
            raise ResearchProviderError(f"unexpected HTTP status={status}")
        raw_text = data.decode("utf-8", errors="replace")
        metadata = dict(hit.metadata)
        title = hit.title
        canonical_hint = ""
        extractor = "DIRECT_TEXT"
        text = raw_text
        if content_type in {"text/html", "application/xhtml+xml"}:
            text, parsed = self.cleaner.clean(raw_text)
            title = parsed.get("title") or title
            canonical_hint = parsed.get("canonical_hint") or parsed.get("og_url") or ""
            extractor = "SWARM_STRUCTURAL_HTML"
            quality = self.router.assess_text(text, content_type=content_type)
            if not quality.acceptable:
                try:
                    browser_text, browser_title, browser_observed = self._browser_fetch(final_url)
                except ResearchProviderError:
                    metadata["phase20q_browser_escalation"] = {"attempted": True, "used": False, "reason": quality.reason}
                else:
                    text = browser_text
                    title = browser_title or title
                    extractor = "PHASE20A_BROWSER"
                    metadata["observed_at"] = browser_observed
                    metadata["phase20q_browser_escalation"] = {"attempted": True, "used": True, "reason": quality.reason}
        elif not (content_type.startswith("text/") or content_type in {"application/json", "application/xml"}):
            raise ResearchProviderError(f"unsupported content type: {content_type}")

        text = text[: self.external_evidence.max_source_text_chars]
        envelope = make_envelope(
            requested_url=hit.url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            raw=data,
            extractor_kind=extractor,
            title=title,
            canonical_hint=canonical_hint,
            metadata={"provider": self.provider_id},
        )
        metadata["phase20q_acquisition"] = envelope.to_dict()
        metadata.setdefault("retrieved_at", envelope.retrieved_at)
        metadata.setdefault("observed_at", envelope.retrieved_at)
        # canonical_hint is untrusted document metadata and is intentionally not
        # used to replace transport final_url here.
        kind, tier = classify_source(final_url, hit.kind)
        return FetchedSourceV1(
            self.provider_id,
            canonicalize_url(final_url),
            title or final_url,
            kind,
            tier,
            "text/plain",
            text,
            len(data),
            metadata,
        )

# Verified compatibility aliases for historical Python interfaces.
Phase20QWebProvider = IntegrityWebProvider
