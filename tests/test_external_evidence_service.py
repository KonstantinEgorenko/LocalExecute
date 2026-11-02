from types import SimpleNamespace

from harness.external_evidence.models import ResearchChannel, ResearchQueryV1, ResearchRequestV1
from harness.external_evidence.service import AutonomousResearchService


class FakeReasoner:
    def __init__(self):
        self.calls = []

    def create_request(self, **kwargs):
        self.calls.append(kwargs)
        return ResearchRequestV1(
            kwargs["research_id"], kwargs["question"], tuple(kwargs["required_topics"]),
            (ResearchQueryV1("q1", "test", ResearchChannel.WEB),), kwargs.get("root_task_sha256", ""),
        )


class FakeBridge:
    def __init__(self):
        self.requests = []

    def research(self, request):
        self.requests.append(request)
        return SimpleNamespace(sufficient=True, research_id=request.research_id)


def test_autonomous_service_builds_request_then_researches():
    reasoner = FakeReasoner()
    bridge = FakeBridge()
    service = AutonomousResearchService(reasoner=reasoner, bridge=bridge)
    result = service.research_question(research_id="r1", question="question", required_topics=("topic",), root_task_sha256="a" * 64)
    assert result.sufficient is True
    assert reasoner.calls[0]["question"] == "question"
    assert bridge.requests[0].research_id == "r1"
