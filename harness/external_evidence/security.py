'Read-only network safety policy for external evidence external fetches.'
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class ResearchNetworkPolicyError(ValueError):
    pass


def canonicalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ResearchNetworkPolicyError("URL must be non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() not in {"http", "https"}:
        raise ResearchNetworkPolicyError("only http/https URLs are allowed")
    if not parts.hostname:
        raise ResearchNetworkPolicyError("URL hostname is required")
    if parts.username is not None or parts.password is not None:
        raise ResearchNetworkPolicyError("userinfo in URL is forbidden")
    host = parts.hostname.casefold().rstrip(".")
    port = parts.port
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (parts.scheme.casefold() == "https" and port == 443)
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None or default_port else f"{display_host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.casefold(), netloc, path, parts.query, ""))


def _address_allowed(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global)


def validate_external_url(url: str, *, block_private: bool = True) -> str:
    normalized = canonicalize_url(url)
    if not block_private:
        return normalized
    host = urlsplit(normalized).hostname
    assert host is not None
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        direct = None
    if direct is not None:
        if not direct.is_global:
            raise ResearchNetworkPolicyError(f"non-global target forbidden: {host}")
        return normalized
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ResearchNetworkPolicyError(f"DNS resolution failed for {host}: {exc}") from exc
    addresses = {item[4][0] for item in results}
    if not addresses or any(not _address_allowed(item) for item in addresses):
        raise ResearchNetworkPolicyError(f"target resolves to non-global address: {host}")
    return normalized
