from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

from .models import (
    BrowserObservation, EvidenceFragmentV1, FileExtractionV1,
    MediaObservationV1, NormalizedSourceV1,
)
from .policy import ResearchEnvironmentPolicy


def _id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:24]}"


def _canonical_http(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme.casefold(), netloc, parts.path or "/", parts.query, ""))


class EvidenceBridge:
    def __init__(self, policy: ResearchEnvironmentPolicy):
        self.policy = policy

    def from_browser(self, observation: BrowserObservation, *, tier: str | None = None):
        canonical = _canonical_http(observation.url)
        source_id = _id("src20a", "browser", canonical)
        source = NormalizedSourceV1(
            source_id, canonical, observation.title,
            (tier or self.policy.default_browser_tier).upper(),
            "BROWSER_PAGE", observation.observed_at,
            {
                "provider": "phase20a_browser",
                "action_trail": [x.to_dict() for x in observation.action_trail],
            },
        )
        evidence = ()
        if observation.text.strip():
            evidence = (
                EvidenceFragmentV1(
                    _id("ev20a", source_id, "page_text", observation.text),
                    source_id, observation.text, canonical, observation.observed_at,
                    {"provider": "phase20a_browser"},
                ),
            )
        return source, evidence

    def from_file(self, extraction: FileExtractionV1, *, tier="SECONDARY", parent_source_id=""):
        source_id = _id("src20a", "file", extraction.artifact_uri)
        title = extraction.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        source = NormalizedSourceV1(
            source_id, extraction.artifact_uri, title, tier.upper(),
            "LOCAL_ARTIFACT", extraction.extracted_at,
            {
                "provider": "phase20a_file",
                "sha256": extraction.sha256,
                "media_type": extraction.media_type,
                "parent_source_id": parent_source_id,
            },
        )
        evidence = tuple(
            EvidenceFragmentV1(
                _id("ev20a", source_id, item.fragment_id, item.text),
                source_id, item.text, item.locator, extraction.extracted_at,
                {"provider": "phase20a_file", **dict(item.metadata)},
            )
            for item in extraction.fragments
        )
        return source, evidence

    def from_media(self, observation: MediaObservationV1, *, tier="SECONDARY", parent_source_id=""):
        source_id = _id("src20a", "media", observation.artifact_uri)
        title = observation.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        source = NormalizedSourceV1(
            source_id, observation.artifact_uri, title, tier.upper(),
            "MEDIA_ARTIFACT", observation.observed_at,
            {
                "provider": "phase20a_media",
                "sha256": observation.sha256,
                "media_type": observation.media_type,
                "parent_source_id": parent_source_id,
                **dict(observation.metadata),
            },
        )
        evidence = ()
        if observation.semantic_text.strip():
            evidence = (
                EvidenceFragmentV1(
                    _id(
                        "ev20a", source_id, observation.semantic_method,
                        observation.semantic_text
                    ),
                    source_id, observation.semantic_text, "semantic-analysis",
                    observation.observed_at,
                    {
                        "provider": "phase20a_media",
                        "semantic_method": observation.semantic_method,
                    },
                ),
            )
        return source, evidence
