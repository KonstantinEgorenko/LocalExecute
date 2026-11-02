from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import yaml

@dataclass(frozen=True, slots=True)
class ResearchEnvironmentPolicy:
    headless: bool
    navigation_timeout_ms: int
    action_timeout_ms: int
    max_observation_chars: int
    max_interactive_elements: int
    max_action_trail: int
    max_download_bytes: int
    allowed_schemes: tuple[str, ...]
    blocked_host_suffixes: tuple[str, ...]
    blocked_hosts: tuple[str, ...]
    max_file_bytes: int
    max_total_chars: int
    max_fragment_chars: int
    supported_extensions: tuple[str, ...]
    max_frames: int
    seconds_per_frame: int
    allow_semantic_inference_without_analyzer: bool
    default_browser_tier: str
    downloaded_file_inherits_parent_tier: bool
    live_browser_url: str
    require_playwright: bool
    require_chromium: bool

    def __post_init__(self):
        positive = (
            "navigation_timeout_ms", "action_timeout_ms", "max_observation_chars",
            "max_interactive_elements", "max_action_trail", "max_download_bytes",
            "max_file_bytes", "max_total_chars", "max_fragment_chars",
            "max_frames", "seconds_per_frame",
        )
        for name in positive:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if set(self.allowed_schemes) - {"http", "https"}:
            raise ValueError("Phase20A network schemes are restricted to http/https")

    @classmethod
    def from_project_root(cls, root):
        raw = normalize_configuration_document(yaml.safe_load((resolve_config_path(Path(root) / "config/phase20a.yaml")).read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase20a.yaml requires schema_version: 1")
        body = raw["research_environment_v1"]
        b, f, m, e, live = body["browser"], body["files"], body["media"], body["evidence"], body["live_acceptance"]
        return cls(
            bool(b["headless"]), int(b["navigation_timeout_ms"]), int(b["action_timeout_ms"]),
            int(b["max_observation_chars"]), int(b["max_interactive_elements"]),
            int(b["max_action_trail"]), int(b["max_download_bytes"]),
            tuple(str(x).casefold() for x in b["allowed_schemes"]),
            tuple(str(x).casefold() for x in b["blocked_host_suffixes"]),
            tuple(str(x).casefold() for x in b["blocked_hosts"]),
            int(f["max_file_bytes"]), int(f["max_total_chars"]), int(f["max_fragment_chars"]),
            tuple(str(x).casefold() for x in f["supported_extensions"]),
            int(m["max_frames"]), int(m["seconds_per_frame"]),
            bool(m["allow_semantic_inference_without_analyzer"]),
            str(e["default_browser_tier"]).upper(),
            bool(e["downloaded_file_inherits_parent_tier"]),
            str(live["browser_url"]), bool(live["require_playwright"]),
            bool(live["require_chromium"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20APolicy = ResearchEnvironmentPolicy
