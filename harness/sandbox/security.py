from __future__ import annotations

import hashlib
import re
import os
import subprocess
from pathlib import Path

from .models import NetworkProfile
from .policy import SandboxPolicy


class SandboxSecurityError(ValueError):
    pass


def fingerprint_worktree(path: Path) -> str:
    target = Path(path).resolve()
    payload = str(target).casefold().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sanitize_sandbox_id(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "")).strip(".-")
    if not text:
        raise SandboxSecurityError("sandbox_id is empty after normalization")
    return text[:96]


def validate_external_worktree(worktree: Path, control_root: Path) -> Path:
    candidate = Path(worktree).resolve()
    control = Path(control_root).resolve()
    if not candidate.is_dir():
        raise SandboxSecurityError(f"worktree directory missing: {candidate}")
    if candidate == control or control in candidate.parents:
        raise SandboxSecurityError(
            f"candidate worktree must be external to control repository: {candidate}"
        )
    return candidate



def _git_path(worktree: Path, value: str) -> Path:
    raw = Path(value.strip())
    if not raw.is_absolute():
        raw = worktree / raw
    return raw.resolve()


def validate_linked_git_worktree(worktree: Path) -> None:
    target = Path(worktree).resolve()
    inside = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip().casefold() != "true":
        raise SandboxSecurityError(f"not a Git worktree: {target}")

    git_dir_result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    common_result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if git_dir_result.returncode != 0 or common_result.returncode != 0:
        raise SandboxSecurityError(f"unable to inspect Git worktree metadata: {target}")

    git_dir = _git_path(target, git_dir_result.stdout)
    common_dir = _git_path(target, common_result.stdout)
    if git_dir == common_dir:
        raise SandboxSecurityError(
            f"workspace must be a linked Git worktree, not the primary checkout: {target}"
        )


def canonical_host_mount_path(value: str) -> str:
    """Normalize Windows, WSL, and Docker Desktop forms of one host path."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise SandboxSecurityError("mount source path is empty")

    lowered = raw.casefold()
    for prefix in (
        "/run/desktop/mnt/host/",
        "/host_mnt/",
        "/mnt/",
    ):
        if lowered.startswith(prefix):
            remainder = raw[len(prefix):].lstrip("/")
            parts = remainder.split("/", 1)
            if parts and len(parts[0]) == 1 and parts[0].isalpha():
                drive = parts[0].casefold()
                tail = parts[1] if len(parts) > 1 else ""
                return f"{drive}:/{tail}".rstrip("/").casefold()

    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        drive = raw[0].casefold()
        tail = raw[2:].lstrip("/")
        return f"{drive}:/{tail}".rstrip("/").casefold()

    return raw.rstrip("/").casefold()


def mount_source_matches_host(mount_source: str, host_path: Path) -> bool:
    source_text = str(mount_source or "").strip()
    host = Path(host_path).resolve()

    # Windows can expose the same directory as a Unicode long path and as an
    # 8.3 DOS alias. When both names are visible on the host, filesystem
    # identity is authoritative and remains fail-closed.
    source_path = Path(source_text)
    try:
        if source_path.exists() and host.exists() and os.path.samefile(source_path, host):
            return True
    except OSError:
        pass

    return canonical_host_mount_path(source_text) == canonical_host_mount_path(
        str(host)
    )

def docker_network_name(profile: NetworkProfile, policy: SandboxPolicy) -> str:
    if profile == NetworkProfile.NONE:
        return "none"
    if profile == NetworkProfile.RESEARCH_ONLY:
        if not policy.research_proxy_configured:
            raise SandboxSecurityError(
                "RESEARCH_ONLY is fail-closed until an egress research proxy is configured"
            )
        raise SandboxSecurityError(
            "RESEARCH_ONLY proxy wiring is reserved for a later additive integration"
        )
    if profile == NetworkProfile.PACKAGE_INSTALL:
        if not policy.allow_package_install_profile:
            raise SandboxSecurityError(
                "PACKAGE_INSTALL network profile is disabled by policy"
            )
        return "bridge"
    raise SandboxSecurityError(f"unsupported network profile: {profile}")


def ensure_image_allowed(image: str, policy: SandboxPolicy) -> str:
    value = str(image)
    if value not in policy.allowed_images:
        raise SandboxSecurityError(f"image is not allowlisted: {value}")
    return value
