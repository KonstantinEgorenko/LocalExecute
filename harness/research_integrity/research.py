from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness.research_remediation.models import RemediationTargetV1
from harness.research_remediation.policy import ResearchRemediationPolicy
from harness.runtime_hardening.policy import RuntimeHardeningPolicy

from .models import TargetEntityV1


class IntegrityTargetedResearchRunner:
    def __init__(self, root: Path, policy: ResearchRemediationPolicy) -> None:
        self.root = Path(root)
        self.policy = policy
        self.hardening = RuntimeHardeningPolicy.from_project_root(self.root)

    def run(
        self,
        *,
        base_research_id: str,
        target: RemediationTargetV1,
        ordinal: int,
        target_entity: TargetEntityV1 | None = None,
    ) -> Path:
        safe_claim = target.claim_id.replace(":", "-").replace("/", "-")
        research_id = f"{base_research_id}-20q-{ordinal:02d}-{safe_claim}"
        cmd = [
            sys.executable,
            "-m",
            "scripts.run_research_integrity_operational_research",
            "--research-id",
            research_id,
            "--question",
            target.research_question,
        ]
        for topic in target.required_topics:
            cmd.extend(["--topic", topic])
        if target_entity is not None and target_entity.github_full_name:
            cmd.extend(["--target-github-repo", target_entity.github_full_name])
        subprocess.run(cmd, cwd=self.root, check=True, timeout=self.hardening.research_timeout_seconds)
        path = self.root / "state" / "research" / "operational" / research_id / "research_report.json"
        if not path.is_file():
            raise RuntimeError(f"Phase20Q targeted research did not produce report: {path}")
        return path

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20QTargetedResearchRunner = IntegrityTargetedResearchRunner
