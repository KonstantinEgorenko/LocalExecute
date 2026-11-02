from __future__ import annotations
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from .policy import ResearchEnvironmentPolicy

class UnsafeResearchUrl(ValueError):
    pass

@dataclass
class ResearchUrlPolicy:
    policy: ResearchEnvironmentPolicy
    _dns_cache: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def validate_top_level(self, url: str) -> str:
        normalized = self._normalize(url)
        parts = urlsplit(normalized)
        if parts.scheme.casefold() not in self.policy.allowed_schemes:
            raise UnsafeResearchUrl(f"scheme blocked: {parts.scheme}")
        self._validate_host(parts.hostname or "")
        return normalized

    def validate_network_request(self, url: str) -> None:
        parts = urlsplit(url)
        scheme = parts.scheme.casefold()
        if scheme in {"data", "blob", "about"}:
            return
        if scheme not in self.policy.allowed_schemes:
            raise UnsafeResearchUrl(f"network request scheme blocked: {scheme}")
        self._validate_host(parts.hostname or "")

    @staticmethod
    def _normalize(url: str) -> str:
        text = str(url or "").strip()
        if not text:
            raise UnsafeResearchUrl("URL is empty")
        parts = urlsplit(text)
        if parts.username or parts.password:
            raise UnsafeResearchUrl("URL userinfo is blocked")
        host = (parts.hostname or "").casefold()
        if not host:
            raise UnsafeResearchUrl("URL hostname missing")
        port = parts.port
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parts.scheme.casefold(), netloc, parts.path or "/", parts.query, ""))

    def _validate_host(self, host: str) -> None:
        name = host.rstrip(".").casefold()
        if name in self.policy.blocked_hosts:
            raise UnsafeResearchUrl(f"hostname blocked: {name}")
        if any(name.endswith(suffix) for suffix in self.policy.blocked_host_suffixes):
            raise UnsafeResearchUrl(f"hostname suffix blocked: {name}")
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            for address in self._resolve(name):
                self._validate_ip(ipaddress.ip_address(address))
        else:
            self._validate_ip(ip)

    def _resolve(self, host: str) -> tuple[str, ...]:
        if host in self._dns_cache:
            return self._dns_cache[host]
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeResearchUrl(f"DNS resolution failed for {host}: {exc}") from exc
        addresses = tuple(sorted({str(x[4][0]) for x in infos if x[4]}))
        if not addresses:
            raise UnsafeResearchUrl(f"hostname did not resolve: {host}")
        self._dns_cache[host] = addresses
        return addresses

    @staticmethod
    def _validate_ip(ip):
        if not ip.is_global:
            raise UnsafeResearchUrl(f"non-global address blocked: {ip}")
