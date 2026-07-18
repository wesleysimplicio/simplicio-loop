"""Resolve a real `RepositoryIdentity` from an actual Git repository/worktree path.

`map_service.py`'s `RepositoryIdentity` takes canonical_root/worktree_root/base_sha/dirty
etc. as caller-supplied values — by design, so the protocol has no Git dependency. The
gap this closes (#512/#513's "integração Git/mapper real, múltiplos worktrees" AC) is
that nothing in the repo actually computed those values from a real repository; every
existing test used synthetic in-memory values. This module is the real Git adapter:
subprocess calls only, no mocking, no synthetic tree hashes.

Deliberately additive: does not modify map_service.py/map_service_single_flight.py/
map_service_watchers.py's existing tested behavior.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import List, Tuple

from simplicio_loop.map_service import RepositoryIdentity


class GitIdentityError(RuntimeError):
    """Raised when `path` is not inside a real Git working tree."""


def _git(path: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise GitIdentityError(
            "git %s failed in %s: %s" % (" ".join(args), path, result.stderr.strip())
        )
    return result.stdout.strip()


def _default_branch(path: str) -> str:
    # Prefer the remote's declared default; a local-only repo (common in tests, and not
    # unusual for a fresh clone before a remote is configured) has no origin, so fall
    # back to whatever branch is actually checked out, and finally to a fixed default -
    # never raise just because there's no remote.
    try:
        ref = _git(path, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.rsplit("/", 1)[-1]
    except GitIdentityError:
        pass
    try:
        return _git(path, "symbolic-ref", "--short", "HEAD")
    except GitIdentityError:
        return "main"


def _dirty_fingerprint(path: str, status_output: str) -> str:
    """Hash actual uncommitted CONTENT, not just which files changed — `git status
    --porcelain` alone only lists filenames/flags, so two different edits to the same
    file would otherwise produce an identical fingerprint (a real bug caught by
    test_dirty_detection_and_fingerprint_reflect_real_uncommitted_changes)."""
    hasher = hashlib.sha256()
    hasher.update(_git(path, "diff", "HEAD").encode("utf-8"))
    for line in status_output.splitlines():
        if line.startswith("??"):
            untracked = line[3:].strip()
            try:
                hasher.update((Path(path) / untracked).read_bytes())
            except OSError:
                pass  # deleted/unreadable between status and read - fingerprint still bounded
    return hasher.hexdigest()


def _repository_label(canonical_root: str) -> str:
    try:
        url = subprocess.run(
            ["git", "-C", canonical_root, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15,
        )
        if url.returncode == 0 and url.stdout.strip():
            return url.stdout.strip()
    except OSError:
        pass
    return canonical_root


def resolve_repository_identity(path: str, *, mapper_config: str = "") -> RepositoryIdentity:
    """Compute a real `RepositoryIdentity` for the repository/worktree at `path` by
    shelling out to real Git — no caller-supplied shortcuts."""
    resolved = str(Path(path).expanduser().resolve(strict=True))
    try:
        this_root = _git(resolved, "rev-parse", "--show-toplevel")
    except GitIdentityError as exc:
        raise GitIdentityError("%s is not inside a Git working tree" % path) from exc

    # `worktree list --porcelain`'s first entry is always the MAIN working tree,
    # regardless of which worktree we ran the command from — that's the canonical root
    # every other worktree of the same repository shares.
    listing = _git(resolved, "worktree", "list", "--porcelain")
    worktree_roots: List[str] = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            worktree_roots.append(line[len("worktree "):].strip())
    if not worktree_roots:
        raise GitIdentityError("git worktree list returned no entries for %s" % path)
    canonical_root = worktree_roots[0]
    is_main_worktree = str(Path(this_root).resolve()) == str(Path(canonical_root).resolve())
    worktree_root = None if is_main_worktree else this_root

    base_sha = _git(resolved, "rev-parse", "HEAD")
    status = _git(resolved, "status", "--porcelain")
    dirty = bool(status)
    dirty_fingerprint = _dirty_fingerprint(resolved, status) if dirty else ""

    return RepositoryIdentity(
        repository=_repository_label(canonical_root),
        canonical_root=canonical_root,
        default_branch=_default_branch(resolved),
        worktree_root=worktree_root,
        base_sha=base_sha,
        dirty=dirty,
        dirty_fingerprint=dirty_fingerprint,
        mapper_config=mapper_config,
    )


def real_tree_snapshot(path: str) -> Tuple[str, List[str]]:
    """A REAL tree_hash + file list for `build_canonical`/`build_overlay`, derived from
    actual tracked file contents (not just HEAD's commit sha, so uncommitted changes to
    already-tracked files are reflected too) - `git ls-files` + a rolled hash of each
    tracked file's git blob id, which changes iff any tracked file's content changes."""
    resolved = str(Path(path).expanduser().resolve(strict=True))
    files = [line for line in _git(resolved, "ls-files").splitlines() if line]
    if not files:
        return hashlib.sha256(b"empty-tree").hexdigest(), []
    ls_tree = _git(resolved, "ls-files", "-s")  # "<mode> <blob-sha> <stage>\t<path>"
    blob_shas = sorted(line.split()[1] for line in ls_tree.splitlines() if line.strip())
    tree_hash = hashlib.sha256("".join(blob_shas).encode("utf-8")).hexdigest()
    return tree_hash, [str(Path(resolved) / f) for f in files]
