import json

from harness.external_evidence.controller import ResearchController
from harness.external_evidence.models import ResearchChannel
from harness.external_evidence.store import JsonResearchStore
from tests.external_evidence_helpers import FixtureSynthesis, fixture_providers, policy, request


def test_store_writes_fingerprinted_report_and_graph(tmp_path):
    web, gh = fixture_providers()
    store = JsonResearchStore(tmp_path / "research")
    controller = ResearchController(policy=policy(), discovery={ResearchChannel.WEB.value: web, ResearchChannel.GITHUB_REPOSITORIES.value: gh}, fetchers={web.provider_id: web, gh.provider_id: gh}, synthesis=FixtureSynthesis(), store=store)
    controller.run(request())
    report = json.loads((tmp_path / "research/research-1/research_report.json").read_text())
    graph = json.loads((tmp_path / "research/research-1/evidence_graph.json").read_text())
    assert len(report["report_fingerprint"]) == 64
    assert len(graph["evidence_graph_fingerprint"]) == 64
