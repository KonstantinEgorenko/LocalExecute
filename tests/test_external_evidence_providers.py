from harness.external_evidence.models import ResearchChannel, ResearchQueryV1, SourceKind
from harness.external_evidence.providers import GitHubRestProvider, SearxNgProvider


def test_searxng_json_parser():
    q = ResearchQueryV1("q1", "test", ResearchChannel.WEB)
    hits = SearxNgProvider.parse_results({"results": [{"title": "A", "url": "https://example.com/a", "content": "x", "engine": "duckduckgo"}]}, q, limit=5)
    assert len(hits) == 1
    assert hits[0].kind is SourceKind.WEB_PAGE
    assert hits[0].metadata["engine"] == "duckduckgo"


def test_github_repository_parser():
    q = ResearchQueryV1("q1", "agent", ResearchChannel.GITHUB_REPOSITORIES)
    raw = {"items": [{"html_url": "https://github.com/acme/repo", "full_name": "acme/repo", "description": "d", "default_branch": "main", "url": "https://api.github.com/repos/acme/repo"}]}
    hits = GitHubRestProvider.parse_search(raw, q, limit=5)
    assert hits[0].kind is SourceKind.GITHUB_REPOSITORY
    assert hits[0].metadata["full_name"] == "acme/repo"


def test_github_code_parser():
    q = ResearchQueryV1("q1", "symbol", ResearchChannel.GITHUB_CODE)
    raw = {"items": [{"html_url": "https://github.com/acme/repo/blob/main/a.py", "path": "a.py", "url": "https://api.github.com/repositories/1/contents/a.py", "repository": {"full_name": "acme/repo"}}]}
    hits = GitHubRestProvider.parse_search(raw, q, limit=5)
    assert hits[0].kind is SourceKind.GITHUB_CODE
