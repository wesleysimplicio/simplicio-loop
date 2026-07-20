import os
import json
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import (
    HubDaemon,
    HubError,
    HubEnvelope,
    HubProtocolError,
    HubSocketClient,
    HubSocketServer,
    default_endpoint,
    doctor,
    main as hub_main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_unix_socket_end_to_end_and_permission_bits(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
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
            incompatible = client.request_raw(json.dumps({
                "schema": "simplicio.hub-ipc/v1", "version": 99,
                "request_id": "bad-version", "method": "ping", "payload": {},
            }))
            assert incompatible["ok"] is False
        finally:
            server.shutdown()
            daemon.stop()


def test_unix_socket_version_mismatch_leaves_daemon_state_untouched(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            client = HubSocketClient(socket_path)
            client.request("r1", "register", client_id="cli")
            client.request("r2", "submit", client_id="cli", job_id="job-1")

            before_clients = set(daemon.clients)
            before_job1 = daemon.queue.get_row(daemon.queue.find_task_id("job-1"))["payload"]

            incompatible = client.request_raw(json.dumps({
                "schema": "simplicio.hub-ipc/v1", "version": 99,
                "request_id": "bad-version", "method": "submit",
                "payload": {"client_id": "cli", "job_id": "job-2"},
            }))
            assert incompatible["ok"] is False
            assert "error" in incompatible

            wrong_schema = client.request_raw(json.dumps({
                "schema": "simplicio.hub-ipc/v2", "version": 1,
                "request_id": "bad-schema", "method": "submit",
                "payload": {"client_id": "cli", "job_id": "job-3"},
            }))
            assert wrong_schema["ok"] is False

            assert daemon.clients == before_clients
            assert daemon.queue.get_row(daemon.queue.find_task_id("job-1"))["payload"] == before_job1
            assert daemon.queue.find_task_id("job-2") is None
            assert daemon.queue.find_task_id("job-3") is None

            still_alive = client.request("r3", "ping")
            assert still_alive["ok"] is True
            assert still_alive["started"] is True
        finally:
            server.shutdown()
            daemon.stop()


def test_unix_socket_20_concurrent_clients_no_crash_correct_singleton(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            def worker(index: int) -> dict:
                client_id = "client-%d" % index
                job_id = "job-%d" % index
                client = HubSocketClient(socket_path, timeout=10.0)
                reg = client.request("reg-%d" % index, "register", client_id=client_id)
                client.request("sub-%d" % index, "submit", client_id=client_id, job_id=job_id)
                claim = client.request("claim-%d" % index, "claim", client_id=client_id, job_id=job_id)
                progress = client.request(
                    "prog-%d" % index, "progress", client_id=client_id, job_id=job_id, progress=50
                )
                result = client.request(
                    "res-%d" % index, "result", client_id=client_id, job_id=job_id, result={"ok": True}
                )
                reconnect = HubSocketClient(socket_path, timeout=10.0)
                pinged = reconnect.request("ping-%d" % index, "ping")
                return {
                    "client_id": client_id,
                    "job_id": job_id,
                    "register_ok": reg["ok"],
                    "claim_state": claim["job"]["state"],
                    "progress_state": progress["job"]["state"],
                    "result_state": result["job"]["state"],
                    "ping_ok": pinged["ok"],
                }

            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(worker, range(20)))

            assert len(results) == 20
            assert all(r["register_ok"] for r in results)
            assert all(r["claim_state"] == "claimed" for r in results)
            assert all(r["progress_state"] == "running" for r in results)
            assert all(r["result_state"] == "completed" for r in results)
            assert all(r["ping_ok"] for r in results)
            assert {r["client_id"] for r in results} == {"client-%d" % i for i in range(20)}
            assert len(daemon.clients) == 20
            assert daemon.queue.count() == 20
            assert all(
                daemon.queue.get_row(daemon.queue.find_task_id("job-%d" % i))["payload"]["state"] == "completed"
                for i in range(20)
            )

            second_daemon = HubDaemon(lock_path)
            with pytest.raises(HubError):
                second_daemon.start()
            assert daemon.started is True
            assert len(daemon.clients) == 20
        finally:
            server.shutdown()
            daemon.stop()


def test_socket_shutdown_is_idempotent_and_removes_file(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX")
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


def test_execute_process_over_hub_uses_safe_supervisor_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        try:
            response = daemon.handle(HubEnvelope(
                "exec-1",
                "execute",
                {"process_spec": {
                    "schema": "simplicio.process-spec/v1",
                    "argv": [sys.executable, "-c", "print('hub-ok')"],
                    "timeout_seconds": 5,
                }},
            ))
            assert response["ok"] is True
            assert response["backend"] in {"rust", "python-fallback"}
            assert response["result"]["stdout"].strip() == "hub-ok"
            assert response["result"]["schema"] == "simplicio.process-result/v1"
        finally:
            daemon.stop()


def test_execute_rejects_invalid_spec_and_enforces_deadline() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        try:
            with pytest.raises(HubProtocolError, match="shell execution"):
                daemon.handle(HubEnvelope(
                    "exec-bad", "execute", {"process_spec": {
                        "argv": [sys.executable, "-c", "print('no')"],
                        "shell": True,
                    }},
                ))
            response = daemon.handle(HubEnvelope(
                "exec-timeout",
                "execute",
                {"process_spec": {
                    "argv": [sys.executable, "-c", "import time; time.sleep(1)"],
                    "timeout_seconds": 0.05,
                }},
            ))
            assert response["result"]["timed_out"] is True
            assert response["result"]["error_code"] == "deadline_exceeded"
        finally:
            daemon.stop()


def test_doctor_reports_unreachable_then_reachable(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX")
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


def test_client_raises_cleanly_when_daemon_absent(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX")
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


def test_subprocess_daemon_singleton_and_client_from_separate_process(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket subprocess coverage runs on POSIX")
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


def test_windows_named_pipe_transport_and_concurrency() -> None:
    if os.name != "nt":
        pytest.skip(
            "CAPABILITY_UNAVAILABLE[named_pipe_windows_only]: "
            "named-pipe transport requires Windows"
        )
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        endpoint = default_endpoint(directory)
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="named-pipe")
        server.start()
        try:
            client = HubSocketClient(endpoint, transport="named-pipe")
            assert client.request("ping", "ping")["started"] is True
            incompatible = client.request_raw(json.dumps({
                "schema": "simplicio.hub-ipc/v1", "version": 99,
                "request_id": "bad-version", "method": "ping", "payload": {},
            }))
            assert incompatible["ok"] is False

            def register(index: int) -> str:
                return HubSocketClient(endpoint, transport="named-pipe").request(
                    "r-%d" % index, "register", client_id="client-%d" % index
                )["client_id"]

            with ThreadPoolExecutor(max_workers=20) as pool:
                clients = list(pool.map(register, range(20)))
            assert len(set(clients)) == 20
            assert doctor(lock_path, endpoint, "named-pipe")["socket_reachable"] is True
        finally:
            server.shutdown()
            daemon.stop()


def test_benchmark_hub_transport_produces_real_latency_receipt(
    require_default_hub_transport,
) -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "benchmark_hub_transport.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["schema"] == "simplicio.hub-transport-benchmark/v1"
    assert payload["requests"] == 100
    assert payload["p50_ms"] > 0
    assert payload["p95_ms"] >= payload["p50_ms"]
    assert payload["throughput_per_second"] > 0
    if os.name != "nt":
        # stdlib `resource` fallback (no psutil dependency) must produce a real RSS on POSIX.
        assert isinstance(payload["rss_bytes"], int) and payload["rss_bytes"] > 0


def test_socket_server_rejects_unknown_transport() -> None:
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        with pytest.raises(ValueError, match="transport must be unix or named-pipe"):
            HubSocketServer(daemon, str(Path(directory) / "hub.sock"), transport="carrier-pigeon")


def test_socket_server_named_pipe_transport_unavailable_off_windows() -> None:
    if os.name == "nt":
        pytest.skip("this asserts the POSIX-only guard against named-pipe transport")
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        server = HubSocketServer(daemon, str(Path(directory) / "hub.pipe"), transport="named-pipe")
        with pytest.raises(RuntimeError, match="named-pipe transport requires Windows"):
            server.start()


def test_doctor_reports_lock_pid_dead_when_lock_payload_is_corrupt() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        Path(lock_path).write_text("not-json-at-all", encoding="utf-8")
        report = doctor(lock_path, socket_path)
        assert report["lock_exists"] is True
        assert report["lock_pid_alive"] is False
        assert "pid" not in report


def test_unix_socket_server_creates_no_thread_per_connection(require_af_unix) -> None:
    """Issue #584: the Unix transport is asyncio-driven -- accepting and serving
    connections must not spawn a new OS thread per connection (only the single
    background thread hosting the event loop itself)."""
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        threads_before_start = threading.active_count()
        server.start()
        threads_after_start = threading.active_count()
        try:
            assert threads_after_start == threads_before_start + 1

            def one(index: int) -> bool:
                return HubSocketClient(socket_path, timeout=10.0).request(
                    "conc-%d" % index, "ping"
                )["ok"]

            with ThreadPoolExecutor(max_workers=32) as pool:
                results = list(pool.map(one, range(64)))
            assert all(results)
            # No thread spawned per accepted connection: still exactly one extra
            # thread (the asyncio event loop) beyond whatever existed before start().
            assert threading.active_count() == threads_after_start
        finally:
            server.shutdown()
            daemon.stop()
        assert threading.active_count() == threads_before_start


def test_unix_socket_stress_200_concurrent_clients_no_latency_regression(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            def worker(index: int) -> float:
                started = time.perf_counter()
                response = HubSocketClient(socket_path, timeout=10.0).request(
                    "stress-%d" % index, "ping"
                )
                assert response["ok"] is True
                return time.perf_counter() - started

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=32) as pool:
                samples = list(pool.map(worker, range(200)))
            elapsed = time.perf_counter() - started
            samples.sort()
            p95 = samples[int(len(samples) * 0.95) - 1]
            assert elapsed < 10.0
            assert p95 < 1.0
        finally:
            server.shutdown()
            daemon.stop()


def test_unix_socket_graceful_shutdown_drains_in_flight_connection(require_af_unix) -> None:
    """A connected-but-not-yet-sending client at shutdown time must not hang the
    shutdown call or leak a dangling task: the async connection task is cancelled
    and awaited by ``_shutdown_async`` before ``shutdown()`` returns."""
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server.start()
        try:
            lingering = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            lingering.connect(socket_path)
            # Never sends a line: this connection is parked inside the server's
            # readline() await when shutdown() is invoked below.
            started = time.perf_counter()
            server.shutdown()
            elapsed = time.perf_counter() - started
            assert elapsed < 5.0
            lingering.close()
        finally:
            daemon.stop()


def test_unix_socket_dispatch_timeout_closes_silent_connection(require_af_unix) -> None:
    if os.name == "nt":
        pytest.skip("Unix socket coverage runs on POSIX; Windows named pipe is covered below")
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock_path)
        daemon.start()
        server = HubSocketServer(daemon, socket_path)
        server._DISPATCH_TIMEOUT_SECONDS = 0.2
        server.start()
        try:
            silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            silent.settimeout(2)
            silent.connect(socket_path)
            # Server closes the connection after the dispatch timeout without a
            # line ever arriving; recv() returns b"" (EOF) instead of hanging.
            assert silent.recv(4096) == b""
            silent.close()
        finally:
            server.shutdown()
            daemon.stop()


def test_cli_doctor_subcommand_reports_no_daemon(capsys: pytest.CaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = str(Path(directory) / "hub.lock")
        socket_path = str(Path(directory) / "hub.sock")
        transport = "named-pipe" if os.name == "nt" else "unix"
        exit_code = hub_main([
            "doctor", "--lock", lock_path, "--endpoint", socket_path, "--transport", transport,
        ])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["lock_exists"] is False
        assert payload["socket_reachable"] is False
