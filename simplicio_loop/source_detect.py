"""GitHub-origin detection and default source-adapter selection (#490).

Makes GitHub the default coordination source of truth for every Simplicio project
whose `origin` remote is hosted on GitHub. Detection runs BEFORE any source adapter
is chosen; when a GitHub `origin` is found the loop coordinates lifecycle (claim,
plan, progress, evidence, PR, merge, close) through GitHub and never silently falls
back to a local backlog. If GitHub auth/transport is unavailable the selection fails
CLOSED with a typed `github_coordination_unavailable` blocker. Explicit non-GitHub
overrides are preserved.

This module only SELECTS an adapter; it never performs remote writes. All writes go
through the chosen `SourceAdapter` (e.g. `GitHubSourceAdapter` in `source_adapter.py`).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from .source_adapter import GitHubSourceAdapter, SourceAdapter

__all__ = [
    "detect_github_origin",
    "select_default_source_adapter",
    "GitHubOriginError",
    "GITHUB_COORDINATION_UNAVAILABLE",
]

GITHUB_COORDINATION_UNAVAILABLE = "github_coordination_unavailable"

# https://github.com/<owner>/<repo>(.git)?
_GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
# git@github.com:<owner>/<repo>(.git)?
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class GitHubOriginError(RuntimeError):
    """Raised when GitHub coordination is required but unavailable (fail-closed)."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _parse_remote(url: str) -> Optional[Tuple[str, str]]:
    """Return (owner, repo) if `url` is a GitHub remote, else None."""
    if not url:
        return None
    m = _GITHUB_HTTPS_RE.match(url.strip()) or _GITHUB_SSH_RE.match(url.strip())
    if not m:
        return None
    owner = m.group("owner")
    repo = m.group("repo")
    if owner in (".", "..") or repo in (".", ".."):
        return None
    return owner, repo


def detect_github_origin(
    repo_path: str | Path = ".",
    *,
    runner: Callable = subprocess.run,
    timeout: int = 20,
) -> Optional[Tuple[str, str]]:
    """Detect a GitHub `origin` remote for `repo_path`.

    Returns (owner, repo) when `origin` points at github.com, else None. Reads only
    local git config — never touches the network. A non-GitHub `origin` (GitLab,
    self-hosted, etc.) returns None so an explicit non-GitHub adapter can take over.
    """
    repo_path = Path(repo_path)
    try:
        completed = runner(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return _parse_remote(completed.stdout.strip())


def _github_auth_available(
    owner: str, repo: str,
    *,
    runner: Callable = subprocess.run,
    timeout: int = 20,
) -> bool:
    """Best-effort check that GitHub auth/transport is usable for this repo.

    Uses `gh issue list` (read-only) which requires both a logged-in `gh` and network
    reachability. Any failure -> coordination is unavailable (fail-closed).
    """
    try:
        completed = runner(
            ["gh", "issue", "list", "--repo", f"{owner}/{repo}", "--state", "open",
             "--limit", "1"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0


def select_default_source_adapter(
    repo_path: str | Path = ".",
    *,
    explicit_override: Optional[str] = None,
    publish_comment_fn: Callable = (lambda *a, **k: None),
    runner: Callable = subprocess.run,
    timeout: int = 20,
    outbox_dir: Optional[str | Path] = None,
) -> SourceAdapter:
    """Select the default `SourceAdapter` for `repo_path` (issue #490).

    Rules:
      * `explicit_override` in ("none", "local") -> raise GitHubOriginError asking the
        caller to supply a non-GitHub adapter (opt-out preserved).
      * A GitHub `origin` is detected -> require GitHub auth; if available return
        `GitHubSourceAdapter`, else fail CLOSED with `github_coordination_unavailable`.
      * No GitHub `origin` -> raise GitHubOriginError(reason_code="no_github_origin") so a
        non-GitHub adapter can be chosen.

    Returns a concrete `SourceAdapter` on success; raises `GitHubOriginError` otherwise.
    """
    if explicit_override in ("none", "local"):
        raise GitHubOriginError(
            "explicit_non_github_override",
            "source coordination explicitly opted out of GitHub; caller must supply a "
            "non-GitHub SourceAdapter",
        )

    detected = detect_github_origin(repo_path, runner=runner, timeout=timeout)
    if detected is None:
        raise GitHubOriginError(
            "no_github_origin",
            "no GitHub `origin` remote detected; use an explicit non-GitHub adapter",
        )
    owner, repo = detected
    if not _github_auth_available(owner, repo, runner=runner, timeout=timeout):
        raise GitHubOriginError(
            GITHUB_COORDINATION_UNAVAILABLE,
            f"GitHub auth/transport unavailable for {owner}/{repo}; coordination blocked",
        )
    return GitHubSourceAdapter(
        owner, repo, publish_comment_fn=publish_comment_fn,
        runner=runner, timeout=timeout, outbox_dir=outbox_dir,
    )
