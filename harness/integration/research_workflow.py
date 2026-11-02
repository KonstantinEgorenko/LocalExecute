'Conditional external evidence research in front of the canonical workflow bridge.'

from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import json
import re
from dataclasses import replace
from typing import Callable

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore
from ..mission import MissionSpecV1, WorkflowExecutionBridge
from ..workflow import WorkflowOutcome, WorkflowResultV1


_RESEARCH_HINT = re.compile(
    r"\b(research|latest|current|compare|web|github)\b|"
    r"\bexternal\s+(research|facts?|sources?|information|data)\b|"
    r"(\u0438\u0441\u0441\u043b\u0435\u0434|\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d|\u0441\u0440\u0430\u0432\u043d|"
    r"\u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442|github|"
    r"\u0432\u043d\u0435\u0448\u043d\w*\s+(\u0438\u0441\u0441\u043b\u0435\u0434|\u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a|"
    r"\u0444\u0430\u043a\u0442|\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446|\u0434\u0430\u043d\u043d))",
    re.IGNORECASE,
)

def bind_production_research_runtime(service, runtime):
    'Reuse the production Ornith runtime inside the external evidence service.\n\n    external evidence core historically constructs its own OrnithRuntime, including the\n    old distinct-endpoint guard. Production owns one physical endpoint and\n    already has the correctly guarded SingleEndpointOrnithRuntime, so the\n    integration layer injects that existing runtime without modifying frozen\n    external evidence semantics.\n    '

    service.reasoner.runtime = runtime
    return service


class ConditionalResearchWorkflowBridge:
    'Bind the durable plan, optionally research, then delegate to workflow.'

    @legacy_keywords({'phase16': 'workflow_bridge'})
    def __init__(
        self,
        *,
        workflow_bridge: WorkflowExecutionBridge,
        scheduler,
        artifact_store: ArtifactStore,
        service_factory: Callable[[], object] | None = None,
    ) -> None:
        self.workflow = workflow_bridge
        self.scheduler = scheduler
        self.artifact_store = artifact_store
        self.service_factory = service_factory

    @staticmethod
    def research_required(spec: MissionSpecV1) -> bool:
        explicit = spec.global_constraints.get("research_required")
        if isinstance(explicit, bool):
            return explicit
        topics = spec.global_constraints.get("research_topics")
        if isinstance(topics, (tuple, list)) and topics:
            return True
        return bool(_RESEARCH_HINT.search(spec.raw_objective))

    def _artifact_id(self, spec: MissionSpecV1) -> str:
        return f"production-research-{spec.mission_id}"

    def _research(self, spec: MissionSpecV1) -> dict:
        artifact_id = self._artifact_id(spec)
        if self.artifact_store.has_artifact(artifact_id):
            return json.loads(self.artifact_store.get_artifact(artifact_id).content)
        if self.service_factory is None:
            raise RuntimeError("research is required but no Phase18 service is configured")
        raw_topics = spec.global_constraints.get("research_topics", ())
        topics = tuple(str(item) for item in raw_topics) if isinstance(raw_topics, (tuple, list)) else ()
        if not topics:
            topics = ("external facts required by the mission",)
        service = self.service_factory()
        observation = service.research_question(
            research_id=f"research-{spec.mission_id}",
            question=spec.raw_objective,
            required_topics=topics,
            root_task_sha256=spec.root_task_sha256,
        )
        payload = {
            "research_id": observation.research_id,
            "sufficient": observation.sufficient,
            "summary": observation.summary,
            "report_ref": observation.report_ref,
            "evidence_graph_ref": observation.evidence_graph_ref,
            "source_count": observation.source_count,
            "claim_count": observation.claim_count,
            "fingerprint": observation.fingerprint,
        }
        artifact = Artifact.from_text(
            artifact_id=artifact_id,
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.TOOL,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            media_type="application/json",
            source="Phase18ResearchMissionObservation",
            created_at="1970-01-01T00:00:00+00:00",
            scope_tags=(spec.goal_id,),
            metadata={"production_role": "RESEARCH", "goal_id": spec.goal_id},
        )
        self.artifact_store.create_artifact(artifact)
        return payload

    def run(self, spec: MissionSpecV1) -> WorkflowResultV1:
        self.scheduler.bind_spec(spec)
        if not self.research_required(spec):
            return self._include_durable_successes(spec, self.workflow.run(spec))
        payload = self._research(spec)
        if not payload["sufficient"]:
            return WorkflowResultV1(
                WorkflowOutcome.NEEDS_ATTENTION,
                spec.goal_id,
                (),
                None,
                0,
                0,
                0,
                f"external research insufficient: {payload['summary']}",
            )
        constraints = dict(spec.global_constraints)
        constraints.update({
            "research_artifact_id": self._artifact_id(spec),
            "research_report_ref": payload["report_ref"],
            "research_evidence_graph_ref": payload["evidence_graph_ref"],
            "research_summary": payload["summary"],
        })
        result = self.workflow.run(replace(spec, global_constraints=constraints))
        return self._include_durable_successes(spec, result)

    def _include_durable_successes(
        self,
        spec: MissionSpecV1,
        result: WorkflowResultV1,
    ) -> WorkflowResultV1:
        'Reconstruct cumulative completion after a process crash.\n\n        Scheduler and the context spine persist a verified Todo before workflow\n        returns. If the process dies in that window, the next invocation skips\n        the Todo. This adapter forwards the durable ordered truth to mission\n        without changing either frozen controller.\n        '

        durable = self.scheduler.succeeded_todo_ids(spec.goal_id)
        completed = tuple(dict.fromkeys((*durable, *result.completed_todo_ids)))
        if completed == result.completed_todo_ids:
            return result
        return replace(result, completed_todo_ids=completed)

# Verified historical attribute spellings, same underlying workflow owner.
ConditionalResearchWorkflowBridge.phase16 = property(lambda self: self.workflow_bridge, lambda self, value: setattr(self, "workflow_bridge", value))
