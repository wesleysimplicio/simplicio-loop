from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def test_cli_status_verify_gc_over_a_real_unix_socket_system(tmp_path: Path) -> None:
    """Real system test: real HubDaemon, real Unix socket, real subprocess CLI calls -
    the #513 'sistema via CLI' AC item."""
    from simplicio_loop.hub_daemon import HubDaemon, HubSocketServer, default_endpoint
    from simplicio_loop.map_service import RepositoryIdentity

    daemon = HubDaemon(str(tmp_path / "hub.lock"))
    daemon.start()
    endpoint = default_endpoint(str(tmp_path))
    server = HubSocketServer(daemon, endpoint, "unix")
    server.start()
    module = [sys.executable, "-m", "simplicio_loop.map_service_cli", "--hub-socket", endpoint]

    def run_cli(*args: str) -> dict:
        completed = subprocess.run(
            module + list(args), capture_output=True, text=True, timeout=15,
        )
        return json.loads(completed.stdout)

    try:
        empty_status = run_cli("status")
        assert empty_status["reachable"] is True
        assert empty_status["status"]["watchers"] == 0

        identity = RepositoryIdentity("owner/project", str(tmp_path), base_sha="sha1")
        daemon.map_registry.register(identity)
        daemon.map_watchers.watch(identity.key, lambda _e: None, debounce_seconds=0.0)
        view = daemon.map_registry.build_canonical(identity.key, tree_hash="tree1")

        status = run_cli("status")
        assert status["status"]["watchers"] == 1
        assert identity.key in status["status"]["identities"]

        verified = run_cli("verify")
        assert verified["reachable"] is True
        assert verified["healthy"] is True

        daemon.map_registry.invalidate(identity.key, reason="cli-test")
        gced = run_cli("gc")
        assert gced["reachable"] is True
        assert gced["removed"] == [view.cache_key]
    finally:
        server.shutdown()
        daemon.stop()


def test_cli_reports_unreachable_hub_honestly(tmp_path: Path) -> None:
    module = [sys.executable, "-m", "simplicio_loop.map_service_cli",
              "--hub-socket", str(tmp_path / "no-such-hub.sock"), "status"]
    completed = subprocess.run(module, capture_output=True, text=True, timeout=15)
    report = json.loads(completed.stdout)
    assert report["reachable"] is False
    assert report["error"]
    assert completed.returncode == 1
