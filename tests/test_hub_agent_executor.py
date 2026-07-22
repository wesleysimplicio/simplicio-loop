from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

from simplicio_loop.hub_daemon import HubDaemon, HubProtocolError, HubSocketClient, HubSocketServer


def _claim(client, request_id="claim", *, code="print('ok')", timeout=2.0, limit=65536, key="k"):
    return client.request(
        request_id, "hub_agent_claim", idempotency_key=key, request={"processes": 1},
        process_spec={"argv": [sys.executable, "-c", code], "timeout_seconds": timeout,
                      "max_output_bytes": limit, "idempotency_key": key},
    )["execution"]


def _terminal(client, handle, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = client.request("status", "hub_agent_status", handle=handle)["execution"]
        if value["state"] in {"completed", "failed", "cancelled", "timed_out", "recovery_unknown"}:
            return value
        time.sleep(0.02)
    raise AssertionError("execution did not become terminal")


def test_real_process_receipt_duplicate_and_collect_after_reopen(require_af_unix):
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        daemon = HubDaemon(lock, resource_limits=None)
        daemon.start()
        server = HubSocketServer(daemon, str(Path(directory) / "hub.sock"), "unix")
        server.start()
        client = HubSocketClient(str(Path(directory) / "hub.sock"), transport="unix")
        try:
            claimed = _claim(client)
            duplicate = _claim(client, "duplicate")
            assert duplicate["handle"] == claimed["handle"]
            assert claimed["namespace"] == "hub-agent/v1"
            assert "hub-agent-process/v1" in client.request(
                "capabilities", "hub_agent_capabilities")["capabilities"]
            client.request("send", "hub_agent_send", handle=claimed["handle"], fence=claimed["fence"])
            done = _terminal(client, claimed["handle"])
            assert done["result"]["stdout"] == "ok\n"
            assert done["receipt"]["cpu_seconds"] is None
            assert done["receipt"]["metrics_reason"] == "unmeasured"
        finally:
            server.shutdown()
            daemon.stop()
        reopened = HubDaemon(lock)
        reopened.start()
        try:
            collected = reopened.hub_agent.collect(claimed["handle"])
            assert collected["state"] == "completed"
        finally:
            reopened.stop()


@pytest.mark.parametrize(
    ("code", "timeout", "limit", "state", "error", "truncated"),
    [
        ("import time; time.sleep(2)", 0.05, 100, "timed_out", "deadline_exceeded", False),
        ("print('x'*1000)", 2.0, 10, "completed", "", True),
    ],
)
def test_timeout_and_truncation(code, timeout, limit, state, error, truncated):
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        try:
            claimed = daemon.hub_agent.claim(
                __import__("simplicio_loop.process_supervisor", fromlist=["ProcessSpec"]).ProcessSpec(
                    (sys.executable, "-c", code), timeout_seconds=timeout, max_output_bytes=limit,
                ),
                __import__("simplicio_loop.hub_governor", fromlist=["ResourceRequest"]).ResourceRequest(),
                idempotency_key="case",
            )
            daemon.hub_agent.send(claimed["handle"], claimed["fence"])
            done = _terminal_in_process(daemon, claimed["handle"])
            assert done["state"] == state
            assert done["result"]["error_code"] == error
            assert done["result"]["truncated"] is truncated
        finally:
            daemon.stop()


def _terminal_in_process(daemon, handle):
    deadline = time.time() + 4
    while time.time() < deadline:
        value = daemon.hub_agent.status(handle)
        if value["state"] in {"completed", "failed", "cancelled", "timed_out", "recovery_unknown"}:
            return value
        time.sleep(0.01)
    raise AssertionError("not terminal")


def test_missing_executable_stale_fence_and_out_of_order():
    from simplicio_loop.hub_governor import ResourceRequest
    from simplicio_loop.process_supervisor import ProcessSpec
    with tempfile.TemporaryDirectory() as directory:
        daemon = HubDaemon(str(Path(directory) / "hub.lock"))
        daemon.start()
        try:
            claimed = daemon.hub_agent.claim(
                ProcessSpec(("simplicio-missing-executable-638",)), ResourceRequest(), idempotency_key="missing",
            )
            with pytest.raises(HubProtocolError, match="stale fence"):
                daemon.handle(__import__("simplicio_loop.hub_daemon", fromlist=["HubEnvelope"]).HubEnvelope(
                    "bad", "hub_agent_send", {"handle": claimed["handle"], "fence": 99}))
            with pytest.raises(Exception, match="not terminal"):
                daemon.hub_agent.collect(claimed["handle"])
            sent = daemon.hub_agent.send(claimed["handle"], claimed["fence"])
            done = _terminal_in_process(daemon, claimed["handle"])
            assert done["result"]["error_code"] == "executable_not_found"
            assert daemon.hub_agent.send(claimed["handle"], sent["fence"])["state"] == "failed"
        finally:
            daemon.stop()


def test_cancel_from_second_socket_and_restart_recovery(require_af_unix):
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, "unix")
        server.start()
        client = HubSocketClient(endpoint, transport="unix")
        claimed = _claim(client, code="import time; time.sleep(5)", key="cancel")
        sent = client.request(
            "send", "hub_agent_send", handle=claimed["handle"], fence=claimed["fence"])["execution"]
        status = HubSocketClient(endpoint, transport="unix").request(
            "status", "hub_agent_status", handle=claimed["handle"])
        assert status["execution"]["state"] == "running"
        cancelled = HubSocketClient(endpoint, transport="unix").request(
            "cancel", "hub_agent_cancel", handle=claimed["handle"], fence=sent["fence"])
        assert cancelled["execution"]["state"] in {"cancelling", "cancelled"}
        assert _terminal(client, claimed["handle"])["state"] == "cancelled"

        held = _claim(client, "held", code="print('never')", key="restart")
        server.shutdown()
        daemon.stop()
        reopened = HubDaemon(lock)
        reopened.start()
        try:
            assert reopened.hub_agent.collect(held["handle"])["state"] == "recovery_unknown"
            assert reopened.service.claim("worker", __import__(
                "simplicio_loop.hub_governor", fromlist=["ResourceRequest"]).ResourceRequest()) is None
        finally:
            reopened.stop()


def test_simulated_oom_backpressure_validation_and_resource_release():
    from simplicio_loop.hub_agent_executor import HubAgentError, HubAgentExecutor, parse_request
    from simplicio_loop.hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest
    from simplicio_loop.process_supervisor import ProcessSpec, PythonProcessAdapter

    class OOMAdapter(PythonProcessAdapter):
        async def run(self, spec, *, lease=None, on_spawned=None):
            raise MemoryError("simulated")

    with tempfile.TemporaryDirectory() as directory:
        governor = ResourceGovernor(ResourceLimits(processes=1))
        executor = HubAgentExecutor(str(Path(directory) / "agent.db"), governor, adapter=OOMAdapter())
        try:
            claimed = executor.claim(
                ProcessSpec((sys.executable, "-c", "pass")), ResourceRequest(processes=1),
                idempotency_key="oom",
            )
            with pytest.raises(HubAgentError, match="backpressure"):
                executor.claim(ProcessSpec(("echo", "x")), ResourceRequest(processes=1),
                               idempotency_key="blocked")
            with pytest.raises(HubAgentError, match="conflicts"):
                executor.claim(ProcessSpec(("echo", "different")), ResourceRequest(),
                               idempotency_key="oom")
            executor.send(claimed["handle"], claimed["fence"])
            done = _wait_executor(executor, claimed["handle"])
            assert done["result"]["error_code"] == "oom"
            assert governor.status()["used"]["processes"] == 0
            with pytest.raises(HubAgentError, match="known resource"):
                parse_request({"bogus": 1})
            with pytest.raises(HubAgentError, match="required"):
                executor.claim(ProcessSpec(("echo", "x")), ResourceRequest(), idempotency_key="")
            with pytest.raises(HubAgentError, match="unknown handle"):
                executor.status("unknown")
        finally:
            executor.close()


def _wait_executor(executor, handle):
    deadline = time.time() + 2
    while time.time() < deadline:
        value = executor.status(handle)
        if value["state"] in {"completed", "failed", "cancelled", "timed_out", "recovery_unknown"}:
            return value
        time.sleep(0.01)
    raise AssertionError("not terminal")


def test_claim_send_status_hot_path_benchmark():
    """Executable benchmark guard; generous ceiling catches accidental blocking I/O."""
    from simplicio_loop.hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest
    from simplicio_loop.hub_agent_executor import HubAgentExecutor
    from simplicio_loop.process_supervisor import ProcessSpec
    with tempfile.TemporaryDirectory() as directory:
        executor = HubAgentExecutor(str(Path(directory) / "agent.db"), ResourceGovernor(ResourceLimits()))
        started = time.perf_counter()
        handles = []
        try:
            for index in range(100):
                item = executor.claim(ProcessSpec((sys.executable, "-c", "pass")), ResourceRequest(),
                                      idempotency_key="bench-%d" % index)
                handles.append(item["handle"])
                executor.status(item["handle"])
            elapsed = time.perf_counter() - started
            print("hub-agent claim+status: %.1f ops/s pmean=%.3fms" % (200 / elapsed, elapsed * 500))
            assert elapsed < 2.0
        finally:
            executor.close()
