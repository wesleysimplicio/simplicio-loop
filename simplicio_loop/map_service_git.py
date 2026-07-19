"""Git-backed repository and worktree discovery for the Map Service.

The registry key is repository-scoped, while each worktree keeps its own HEAD, branch,
and dirty state. All discovery is local and read-only; no remote or mapper invocation is
performed here.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


class GitDiscoveryError(RuntimeError):
    """Raised when a path is not a usable Git worktree."""


def _git(root: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(root), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise GitDiscoveryError("git is unavailable: %s" % exc) from exc
    if result.returncode:
        raise GitDiscoveryError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def _normalize_remote(remote: str) -> str:
    value = str(remote).strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@") and ":" in value:
        value = "https://" + value.split(":", 1)[1]
    return value.rstrip("/").lower()


@dataclass(frozen=True)
class GitWorktree:
    path: str
    head: str
    branch: str = ""
    detached: bool = False


@dataclass(frozen=True)
class RepositoryRecord:
    repository_key: str
    common_dir: str
    remote: str
    first_commit: str
    worktrees: tuple[GitWorktree, ...]


def list_worktrees(root: str) -> List[GitWorktree]:
    """Return every worktree Git associates with ``root`` using porcelain output."""
    base = Path(root).expanduser().resolve()
    output = _git(base, ["worktree", "list", "--porcelain"])
    records: List[GitWorktree] = []
    current: Dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        if current.get("worktree") and current.get("HEAD"):
            branch = current.get("branch", "")
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            records.append(GitWorktree(
                path=str(Path(current["worktree"]).resolve()),
                head=current["HEAD"],
                branch=branch,
                detached="detached" in current,
            ))
        current = {}

    for line in output.splitlines() + [""]:
        if not line:
            flush()
        elif line.startswith("worktree "):
            current["worktree"] = line[9:]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line == "detached":
            current["detached"] = "1"
    return records


def discover(root: str) -> RepositoryRecord:
    """Discover one repository identity and all currently attached worktrees."""
    base = Path(_git(Path(root), ["rev-parse", "--show-toplevel"])).resolve()
    common_raw = Path(_git(base, ["rev-parse", "--git-common-dir"]))
    common = (base / common_raw).resolve() if not common_raw.is_absolute() else common_raw.resolve()
    try:
        remote_raw = _git(base, ["config", "--get", "remote.origin.url"])
    except GitDiscoveryError:
        remote_raw = ""
    remote = _normalize_remote(remote_raw)
    first_commit = _git(base, ["rev-list", "--max-parents=0", "HEAD"]).splitlines()[0]
    identity_source = "\n".join((remote, str(common), first_commit)).encode("utf-8")
    repository_key = hashlib.sha256(identity_source).hexdigest()
    return RepositoryRecord(repository_key, str(common), remote, first_commit, tuple(list_worktrees(str(base))))


class RepositoryWorktreeRegistry:
    """Refreshable repository-keyed registry that handles worktree add/remove."""

    def __init__(self) -> None:
        self._records: Dict[str, RepositoryRecord] = {}

    def refresh(self, root: str) -> RepositoryRecord:
        record = discover(root)
        self._records[record.repository_key] = record
        return record

    def get(self, repository_key: str) -> RepositoryRecord:
        try:
            return self._records[str(repository_key)]
        except KeyError as exc:
            raise GitDiscoveryError("unknown repository key") from exc

    def remove_missing_worktrees(self, repository_key: str, root: str) -> RepositoryRecord:
        return self.refresh(root)

    def status(self) -> Dict[str, object]:
        return {
            "schema": "simplicio.map-service-git-registry/v1",
            "repositories": len(self._records),
            "worktrees": sum(len(record.worktrees) for record in self._records.values()),
            "repository_keys": sorted(self._records),
        }


__all__ = [
    "GitDiscoveryError", "GitWorktree", "RepositoryRecord", "RepositoryWorktreeRegistry",
    "discover", "list_worktrees",
]
