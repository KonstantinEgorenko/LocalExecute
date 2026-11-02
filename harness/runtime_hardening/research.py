'runtime hardening targeted research runner using the hardened operational entrypoint.'
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness.research_remediation.models import RemediationTargetV1
from harness.research_remediation.policy import ResearchRemediationPolicy

from .policy import RuntimeHardeningPolicy


class ContextBoundedResearchRunner:
    def __init__(self, root: Path, policy: ResearchRemediationPolicy) -> None:
        self.root = Path(root)
        self.policy = policy
        self.hardening = RuntimeHardeningPolicy.from_project_root(self.root)

    def run(self, *, base_research_id: str, target: RemediationTargetV1, ordinal: int) -> Path:
        safe_claim = target.claim_id.replace(":", "-").replace("/", "-")
        research_id = f"{base_research_id}-19q-{ordinal:02d}-{safe_claim}"
        cmd = [
            sys.executable,
            "-m",
            "scripts.run_runtime_hardening_operational_research",
            "--research-id",
            research_id,
            "--question",
            target.research_question,
        ]
        for topic in target.required_topics:
            cmd.extend(["--topic", topic])
        subprocess.run(
            cmd,
            cwd=self.root,
            check=True,
            timeout=self.hardening.research_timeout_seconds,
        )
        path = self.root / "state" / "research" / "operational" / research_id / "research_report.json"
        if not path.is_file():
            raise RuntimeError(f"Phase20R targeted research did not produce report: {path}")
        return path

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20RTargetedResearchRunner = ContextBoundedResearchRunner
