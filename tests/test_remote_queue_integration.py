import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from simplicio_loop.remote_queue import (
    HTTPRemoteQueue,
    QueueConflict,
    QueueUnavailable,
    SQLiteRemoteQueue,
    _lease_from_json,
    _lease_json,
    create_http_queue_server,
)


def test_idempotent_claim_and_ordered_reconnect_events(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1", {"goal": "docs"})
    a = q.claim("T1", "codex@machine-a", idempotency_key="run:T1", ttl=5)
    assert q.claim("T1", "codex@machine-a", idempotency_key="run:T1") == a
    assert q.heartbeat(a, ttl=5).fencing_token == 1
    q.complete(a, receipt_ref="receipts/T1.json")
    events = q.events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["kind"] == "completed"


def test_expiry_reclaim_increments_fence_and_rejects_stale_worker(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    old = q.claim("T1", "codex@A", idempotency_key="a", ttl=0.01)
    time.sleep(0.03)
    new = q.claim("T1", "claude@B", idempotency_key="b", ttl=5)
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(QueueConflict):
        q.complete(old, receipt_ref="stale")
    q.complete(new, receipt_ref="fresh")


def test_idempotency_key_cannot_be_reused_for_another_task(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    q.enqueue("T2")
    q.claim("T1", "codex@A", idempotency_key="same-key")
    with pytest.raises(QueueConflict):
        q.claim("T2", "codex@A", idempotency_key="same-key")


def test_two_agents_only_one_atomic_claim_wins(tmp_path):
    path = str(tmp_path / "queue.db")
    q = SQLiteRemoteQueue(path)
    q.enqueue("T1")
    results = []
    barrier = threading.Barrier(2)

    def worker(agent):
        local = SQLiteRemoteQueue(path)
        barrier.wait()
        try:
            results.append(local.claim("T1", agent, idempotency_key=agent, ttl=5).agent_id)
        except QueueConflict:
            results.append("conflict")

    threads = [threading.Thread(target=worker, args=("codex@A",)), threading.Thread(target=worker, args=("claude@B",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("conflict") == 1
    assert sum(value != "conflict" for value in results) == 1


def test_http_adapter_preserves_atomic_claims_and_fencing(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    backend.enqueue("T1", {"source": "github", "number": 185})
    server = create_http_queue_server(backend, token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d" % server.server_port
        codex = HTTPRemoteQueue(url, token="secret")
        claude = HTTPRemoteQueue(url, token="secret")
        lease = codex.claim("T1", "codex@A", idempotency_key="run:T1", ttl=5,
                            identity={"agent_id": "codex@A", "runtime": "codex",
                                      "device_id": "laptop-a", "session_id": "s1",
                                      "capabilities": ["claim", "heartbeat", "fencing", "receipts"]})
        codex.assert_active(lease)
        assert codex.heartbeat(lease, ttl=5).fencing_token == 1
        with pytest.raises(QueueConflict):
            claude.claim("T1", "claude@B", idempotency_key="run:T1-other")
        codex.complete(lease, receipt_ref="receipts/T1.json")
        assert codex.events()[-1]["kind"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_unavailable_is_fail_closed():
    with pytest.raises(Exception) as error:
        HTTPRemoteQueue("http://127.0.0.1:1", timeout=0.05).events()
    assert "QueueUnavailable" in type(error.value).__name__


def test_http_client_requires_tls_for_non_loopback_urls():
    with pytest.raises(Exception) as error:
        HTTPRemoteQueue("http://queue.example.internal:8765", timeout=0.05).events()
    assert isinstance(error.value, QueueUnavailable)


def test_network_bind_requires_explicit_tls(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    with pytest.raises(ValueError, match="TLS is required"):
        create_http_queue_server(backend, host="0.0.0.0", token="secret")


def test_server_cli_requires_tls_pair(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "remote_queue_server.py")
    result = subprocess.run(
        [sys.executable, script, "--db", str(tmp_path / "q.db"),
         "--token", "secret", "--tls-certfile", "only-cert.pem"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "must be provided together" in (result.stderr + result.stdout)


def test_server_cli_imports_from_any_cwd_without_module_error(tmp_path):
    # Regression for the import-path bug: the script lives in scripts/ but imports the
    # top-level ``simplicio_loop`` package. Running it as a subprocess used to add only the
    # script's own directory to sys.path, so the import failed with a bare ModuleNotFoundError
    # (exit 1) that masked the intended argparse/ValueError gates. The server now anchors the
    # repo root on sys.path, so a genuine gate (here: partial TLS pair) still surfaces with its
    # intended exit code even when invoked from a neutral working directory.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "remote_queue_server.py")
    result = subprocess.run(
        [sys.executable, script, "--db", str(tmp_path / "q.db"),
         "--token", "secret", "--tls-certfile", "only-cert.pem"],
        capture_output=True, text=True, timeout=10, cwd=str(tmp_path),
    )
    assert result.returncode == 2, result.stderr
    assert "must be provided together" in (result.stderr + result.stdout)
    assert "ModuleNotFoundError" not in result.stderr


def test_claim_retry_after_broken_response_reuses_same_lease_without_duplicate_claim(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    backend.enqueue("T1")
    fail_once = {"claim": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            import json

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path.endswith("/claim"):
                lease = backend.claim(body["task_id"], body["agent_id"], idempotency_key=body["idempotency_key"],
                                      ttl=float(body.get("ttl", 60.0)), identity=body.get("identity"),
                                      capabilities=body.get("capabilities"))
                if fail_once["claim"]:
                    fail_once["claim"] = False
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                raw = json.dumps({"lease": _lease_json(lease)}, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPRemoteQueue("http://127.0.0.1:%d" % server.server_port, timeout=1)
        with pytest.raises(QueueUnavailable):
            client.claim("T1", "codex@A", idempotency_key="run:T1", ttl=5)
        lease = client.claim("T1", "codex@A", idempotency_key="run:T1", ttl=5)
        assert lease.fencing_token == 1
        claimed_events = [event for event in backend.events() if event["kind"] == "claimed"]
        assert len(claimed_events) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_complete_retry_after_broken_response_does_not_duplicate_completion(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    backend.enqueue("T1")
    lease = backend.claim("T1", "codex@A", idempotency_key="run:T1", ttl=5)
    fail_once = {"complete": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            import json

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path.endswith("/complete"):
                try:
                    result = backend.complete(_lease_from_json(body["lease"]), receipt_ref=body["receipt_ref"])
                except QueueConflict as exc:
                    raw = json.dumps({"error": str(exc)}, sort_keys=True).encode("utf-8")
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if fail_once["complete"]:
                    fail_once["complete"] = False
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                raw = json.dumps(result, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPRemoteQueue("http://127.0.0.1:%d" % server.server_port, timeout=1)
        with pytest.raises(QueueUnavailable):
            client.complete(lease, receipt_ref="receipts/T1.json")
        with pytest.raises(QueueConflict, match="stale or expired"):
            client.complete(lease, receipt_ref="receipts/T1.json")
        completed_events = [event for event in backend.events() if event["kind"] == "completed"]
        assert len(completed_events) == 1
        assert completed_events[0]["payload"]["receipt_ref"] == "receipts/T1.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
