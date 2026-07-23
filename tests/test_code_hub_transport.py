"""External-process proof for Code's Loop Hub client wire contract."""

import json
import socket
import tempfile
from pathlib import Path

from simplicio_loop.hub_daemon import HubDaemon, HubSocketServer, CODE_HUB_CLIENT_SCHEMA, CODE_HUB_PROTOCOL


def _request(stream, reader, request_id, method, payload):
    stream.sendall((json.dumps({"schema": CODE_HUB_CLIENT_SCHEMA, "id": request_id,
                                "method": method, "payload": payload}) + "\n").encode())
    line = reader.readline()
    value = json.loads(line)
    assert value["schema"] == CODE_HUB_CLIENT_SCHEMA
    assert value["id"] == request_id
    assert value["ok"] is True, value
    return value["result"]


def test_code_client_contract_uses_one_hub_identity_and_replays_lifecycle():
    with tempfile.TemporaryDirectory() as directory:
        lock = str(Path(directory) / "hub.lock")
        endpoint = str(Path(directory) / "hub.sock")
        daemon = HubDaemon(lock)
        daemon.start()
        server = HubSocketServer(daemon, endpoint, transport="unix")
        server.start()
        try:
            client, workspace, session = "code", "workspace", "session"
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stream.connect(endpoint)
            reader = stream.makefile("rb")
            handshake = _request(stream, reader, 1, "handshake", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "protocol": CODE_HUB_PROTOCOL,
                "client_id": client, "workspace_id": workspace, "session_id": session,
            })
            assert handshake["hub_id"].startswith("loop-hub:")
            assert {item["name"] for item in handshake["services"]} == {"runtime", "mapper", "scheduler", "inference"}
            assert all(item["owner"] == "loop-hub" for item in handshake["services"])
            attached = _request(stream, reader, 2, "attach", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "protocol": CODE_HUB_PROTOCOL,
                "client_id": client, "workspace_id": workspace, "session_id": session,
                "reconnect": False, "cursors": [],
            })
            assert attached["accepted"] is True
            submitted = _request(stream, reader, 3, "submit", {
                "schema": CODE_HUB_CLIENT_SCHEMA, "session_id": session,
                "goal_id": "goal", "turn_id": "turn", "idempotency_key": "turn-key",
                "priority": "interactive", "payload": {},
            })
            assert submitted["workflow_id"] == "turn-key"
            progress = _request(stream, reader, 4, "progress", {
                "workflow_id": "turn-key", "after_sequence": 0,
            })
            assert progress["workflow_id"] == "turn-key"
            cancelled = _request(stream, reader, 5, "cancel", {
                "workflow_id": "turn-key", "session_id": session,
                "idempotency_key": "cancel-key", "reason": "test",
            })
            assert cancelled["state"] == "cancelled"
        finally:
            stream.close()
            server.shutdown()
            daemon.stop()
