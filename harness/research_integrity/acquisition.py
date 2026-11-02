from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from .models import AcquisitionEnvelopeV1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_envelope(*, requested_url: str, final_url: str, status: int, content_type: str, raw: bytes, extractor_kind: str, title: str = "", canonical_hint: str = "", response_headers: Mapping[str, str] | None = None, metadata: Mapping | None = None) -> AcquisitionEnvelopeV1:
    return AcquisitionEnvelopeV1(
        requested_url=requested_url,
        final_url=final_url,
        retrieved_at=utc_now(),
        http_status=int(status),
        content_type=content_type,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_size=len(raw),
        extractor_kind=extractor_kind,
        title=title,
        canonical_hint=canonical_hint,
        response_headers=dict(response_headers or {}),
        metadata=dict(metadata or {}),
    )


@dataclass(frozen=True, slots=True)
class FetchQualityV1:
    acceptable: bool
    reason: str


class AcquisitionRouter:
    def __init__(self, min_text_chars: int) -> None:
        self.min_text_chars = int(min_text_chars)

    def assess_text(self, text: str, *, content_type: str = "text/html") -> FetchQualityV1:
        value = (text or "").strip()
        if len(value) < self.min_text_chars:
            return FetchQualityV1(False, f"text_chars={len(value)} < {self.min_text_chars}")
        lowered = value.casefold()
        shell_markers = ("enable javascript", "__next_data__", "webpack", "loading...")
        if content_type.startswith("text/html") and any(marker in lowered for marker in shell_markers) and len(value) < self.min_text_chars * 3:
            return FetchQualityV1(False, "HTML appears to be a JavaScript shell")
        return FetchQualityV1(True, "direct acquisition sufficient")

    def route(self, direct_text: str, *, content_type: str, browser_fetch: Callable[[], str] | None = None) -> tuple[str, str]:
        quality = self.assess_text(direct_text, content_type=content_type)
        if quality.acceptable or browser_fetch is None:
            return direct_text, "DIRECT"
        browser_text = browser_fetch()
        return browser_text, "BROWSER"
