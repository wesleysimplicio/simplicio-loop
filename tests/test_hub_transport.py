import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import (
    HubDaemon,
    HubError,
    HubSocketClient,
    HubSocketServer,
    doctor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_unix_socket_end_to_end_and_permission_bits() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            mode = stat.S_IMODE(os.stat(socket_path).st_mode)
            assert mode == 0o600

            client = HubSocketClient(socket_path)
            reg = client.request("r1", "register", client_id="cli")
            assert reg["ok"] is True
            assert reg["state"] == "registered"

            sub = client.request("r2", "submit", client_id="cli", job_id="job-1")
            assert sub["job"]["state"] == "queued"

            claim = client.request("r3", "claim", client_id="cli", job_id="job-1")
            assert claim["job"]["state"] == "claimed"

            ping = client.request("r4", "ping", client_id="cli")
            assert ping == {"ok": True, "started": True, "clients": 1, "jobs": 1}

            bad = client.request("r5", "claim", client_id="cli", job_id="missing")
            assert bad["ok"] is False
            assert "error" in bad
        finally:
            server.shutdown()
            daemon.stop()


def test_socket_shutdown_is_idempotent_and_removes_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        assert Path(socket_path).exists()

        server.shutdown()
        assert not Path(socket_path).exists()

        server.shutdown()
        assert not Path(socket_path).exists()

        daemon.stop()
        daemon.stop()


def test_daemon_stop_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        daemon.stop()
        daemon.stop()
        assert not Path(lock_path).exists()


def test_doctor_reports_unreachable_then_reachable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")

        report = doctor(lock_path, socket_path)
        assert report["lock_exists"] is False
        assert report["socket_reachable"] is False

        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            report = doctor(lock_path, socket_path)
            assert report["lock_exists"] is True
            assert report["lock_pid_alive"] is True
            assert report["socket_reachable"] is True
        finally:
            server.shutdown()
            daemon.stop()

        report = doctor(lock_path, socket_path)
        assert report["lock_exists"] is False
        assert report["socket_reachable"] is False


def test_client_raises_cleanly_when_daemon_absent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "hub.sock")
        client = HubSocketClient(socket_path, timeout=1.0)
        with pytest.raises((OSError, HubError)):
            client.request("r1", "ping")


def _spawn_daemon_process(lock_path: str, socket_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / "_hub_daemon_process.py"), lock_path, socket_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc


def _wait_for_line(proc: subprocess.Popen, expected: str, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            return line.strip()
        if proc.poll() is not None:
            break
    raise AssertionError(f"process never printed {expected!r} (exit={proc.poll()})")


def test_subprocess_daemon_singleton_and_client_from_separate_process() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")

        first = _spawn_daemon_process(lock_path, socket_path)
        try:
            line = _wait_for_line(first, "READY")
            assert line == "READY"

            second = _spawn_daemon_process(lock_path, socket_path)
            second_line = _wait_for_line(second, "ALREADY_RUNNING")
            assert second_line == "ALREADY_RUNNING"
            assert second.wait(timeout=5) == 1

            client = HubSocketClient(socket_path, timeout=5.0)
            reg = client.request("r1", "register", client_id="outside-proc")
            assert reg["ok"] is True

            report = doctor(lock_path, socket_path)
            assert report["lock_pid_alive"] is True
            assert report["socket_reachable"] is True
        finally:
            first.terminate()
            first.wait(timeout=10)

        assert not Path(lock_path).exists()
        assert not Path(socket_path).exists()
