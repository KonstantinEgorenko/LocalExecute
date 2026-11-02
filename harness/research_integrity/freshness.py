from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from .models import FreshnessStatus


def _text(value: object) -> str:
    return str(value or "").strip()


def parse_timestamp(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def freshness_timestamp(item: Mapping) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    for key in ("observed_at", "retrieved_at", "fetched_at"):
        value = _text(item.get(key) or metadata.get(key))
        if value:
            return value
    return ""


class FreshnessEvaluatorV2:
    def __init__(self, max_age_hours: int) -> None:
        self.max_age_hours = int(max_age_hours)

    def evaluate(self, item: Mapping, *, now: datetime | None = None, required: bool = True) -> FreshnessStatus:
        if not required:
            return FreshnessStatus.NOT_REQUIRED
        stamp = freshness_timestamp(item)
        if not stamp:
            return FreshnessStatus.UNAVAILABLE
        parsed = parse_timestamp(stamp)
        if parsed is None:
            return FreshnessStatus.INVALID
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age_hours = (current - parsed).total_seconds() / 3600.0
        if age_hours < -1.0:  # materially future-dated is not trustworthy freshness.
            return FreshnessStatus.INVALID
        return FreshnessStatus.FRESH if age_hours <= self.max_age_hours else FreshnessStatus.STALE
