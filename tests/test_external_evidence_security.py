import pytest

from harness.external_evidence.security import ResearchNetworkPolicyError, canonicalize_url, validate_external_url


def test_canonicalize_drops_fragment_and_default_port():
    assert canonicalize_url("HTTPS://Example.COM:443/a#frag") == "https://example.com/a"


def test_file_scheme_forbidden():
    with pytest.raises(ResearchNetworkPolicyError):
        canonicalize_url("file:///etc/passwd")


def test_userinfo_forbidden():
    with pytest.raises(ResearchNetworkPolicyError):
        canonicalize_url("https://user:pass@example.com/")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/",
])
def test_private_ip_targets_forbidden(url):
    with pytest.raises(ResearchNetworkPolicyError):
        validate_external_url(url, block_private=True)
