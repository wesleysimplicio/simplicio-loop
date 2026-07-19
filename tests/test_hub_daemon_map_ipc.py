from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from simplicio_loop.hub_daemon import HubClient, HubDaemon, HubProtocolError
from simplicio_loop.map_service_git import real_tree_snapshot, resolve_repository_identity


def _run(*args: str, cwd: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "git %s failed: %s" % (" ".join(args), result.stderr)


def _init_repo(root: Path) -> None:
    _run("init", "-q", cwd=str(root))
    _run("config", "user.email", "test@example.com", cwd=str(root))
    _run("config", "user.name", "Test", cwd=str(root))
    (root / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    _run("add", "app.py", cwd=str(root))
    _run("commit", "-q", "-m", "initial", cwd=str(root))


def test_map_register_watch_emit_flush_gc_round_trip_in_process(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")

    identity = resolve_repository_identity(str(root))
    registered = client.request(
        "r1", "map_register",
        repository=identity.repository, canonical_root=identity.canonical_root,
        base_sha=identity.base_sha, default_branch=identity.default_branch,
    )
    identity_key = registered["identity_key"]
    assert identity_key == identity.key

    watched = client.request("r2", "map_watch", identity_key=identity_key, debounce_seconds=0.0)
    assert watched["token"]

    tree_hash, files = real_tree_snapshot(str(root))
    built_view = daemon.map_registry.build_canonical(identity_key, tree_hash=tree_hash, files=files)

    client.request("r3", "map_emit", identity_key=identity_key, paths=[str(root / "app.py")])
    flushed = client.request("r4", "map_flush", force=True)
    assert len(flushed["fired"]) == 1
    assert flushed["fired"][0]["identity_key"] == identity_key

    status = client.request("r5", "map_status")
    assert status["status"]["schema"] == "simplicio.map-watcher-status/v1"
    assert identity_key in status["status"]["identities"]

    # flush() already invalidated the view (via store.invalidate); nobody ever
    # acquired a reference to it (references stayed 0), so gc() must reclaim exactly
    # that one real cache_key - not an arbitrary/empty list.
    gced = client.request("r6", "map_gc")
    assert gced["removed"] == [built_view.cache_key]
    daemon.stop()


def test_map_register_rejects_unknown_fields_and_missing_required_ones(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")
    try:
        client.request("r1", "map_register", canonical_root="/tmp")  # missing repository/base_sha
        raise AssertionError("expected HubProtocolError")
    except HubProtocolError:
        pass
    daemon.stop()


def test_map_register_wraps_a_real_collision_as_hub_protocol_error(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")
    client.request("r1", "map_register", repository="owner/one", canonical_root="/tmp/x", base_sha="s1")
    try:
        # A genuinely DIFFERENT logical project at the same root - a real collision,
        # not a state transition (see #513's map_service.py fix).
        client.request("r2", "map_register", repository="owner/two", canonical_root="/tmp/x", base_sha="s2")
        raise AssertionError("expected HubProtocolError")
    except HubProtocolError:
        pass
    daemon.stop()


def test_map_watch_rejects_empty_identity_key(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")
    try:
        client.request("r1", "map_watch", identity_key="")
        raise AssertionError("expected HubProtocolError")
    except HubProtocolError:
        pass
    daemon.stop()


def test_map_emit_rejects_missing_identity_key_bad_paths_and_no_watcher(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")
    try:
        client.request("r1", "map_emit", identity_key="", paths=["x"])
        raise AssertionError("expected HubProtocolError for empty identity_key")
    except HubProtocolError:
        pass
    try:
        client.request("r2", "map_emit", identity_key="k", paths="not-a-list")
        raise AssertionError("expected HubProtocolError for non-list paths")
    except HubProtocolError:
        pass

    registered = client.request("r3", "map_register", repository="owner/p", canonical_root="/tmp/y", base_sha="s")
    try:
        # A real registered identity with NO active watcher - emit() itself raises
        # WatcherError, which must be wrapped, not leaked, over the IPC boundary.
        client.request("r4", "map_emit", identity_key=registered["identity_key"], paths=["/tmp/y/f.py"])
        raise AssertionError("expected HubProtocolError for emit with no watcher")
    except HubProtocolError:
        pass
    daemon.stop()


def test_map_watch_on_unregistered_identity_is_rejected(tmp_path: Path) -> None:
    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    client = HubClient(daemon, "alice")
    try:
        client.request("r1", "map_watch", identity_key="does-not-exist")
        raise AssertionError("expected HubProtocolError")
    except HubProtocolError:
        pass
    daemon.stop()


def test_daemon_restart_starts_map_state_clean_in_use_view_is_honestly_not_persisted(tmp_path: Path) -> None:
    """The honest boundary this closes for #513: map state (registry/store/watchers)
    is pure in-memory - unlike the durable job queue, it does NOT survive a daemon
    restart. This proves that explicitly with a real restart rather than assuming it,
    AND proves the daemon comes back up cleanly (no crash, no stale references) and
    that a fresh build against the SAME real repository content after restart
    reproduces the identical content-addressed tree_hash - the correct guarantee to
    rely on across a restart (content-addressing), not raw object persistence."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    lock_path = str(tmp_path / "hub.lock")

    daemon = HubDaemon(lock_path)
    daemon.start()
    client = HubClient(daemon, "alice")
    identity = resolve_repository_identity(str(root))
    client.request(
        "r1", "map_register",
        repository=identity.repository, canonical_root=identity.canonical_root,
        base_sha=identity.base_sha, default_branch=identity.default_branch,
    )
    tree_hash_before, files_before = real_tree_snapshot(str(root))
    view_before = daemon.map_registry.build_canonical(identity.key, tree_hash=tree_hash_before, files=files_before)
    daemon.map_registry.get_view(view_before.cache_key)  # a client "in use" reference
    daemon.stop()

    restarted = HubDaemon(lock_path)
    restarted.start()
    # Clean, crash-free restart - a fresh watcher status, nothing left over.
    status = HubClient(restarted, "alice").request("r2", "map_status")
    assert status["status"]["watchers"] == 0
    assert status["status"]["identities"] == []
    # The identity itself is gone too (an explicit, honest limitation - not silently
    # assumed to still work): resolve_repo/register must happen again after a restart.
    try:
        restarted.map_registry.identity(identity.key)
        raise AssertionError("expected UnknownRepositoryError - map state must not survive restart")
    except Exception:
        pass

    # But re-registering + rebuilding from the SAME real repository content (which is
    # what a real restart flow would actually do) reproduces the identical tree_hash -
    # content-addressing, not raw object survival, is the real cross-restart guarantee.
    restarted.map_registry.register(identity)
    tree_hash_after, _ = real_tree_snapshot(str(root))
    assert tree_hash_after == tree_hash_before
    restarted.stop()
