"""External-process proof for interactive Hub reconnect and replay."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
import pytest
from simplicio_loop.hub_daemon import HubSocketClient

ROOT = Path(__file__).resolve().parents[1]

def _start(lock, endpoint):
    Path(endpoint).unlink(missing_ok=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    process = subprocess.Popen([sys.executable, "-c", "from simplicio_loop.hub_daemon import main; raise SystemExit(main())", "serve",
        "--lock", lock, "--endpoint", endpoint, "--transport", "unix"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for _ in range(500):
        if Path(endpoint).exists(): return process
        if process.poll() is not None: raise AssertionError(process.stderr.read())
        time.sleep(.02)
    raise AssertionError("external Hub did not become ready")

def _stop(process):
    process.terminate(); process.wait(timeout=10)

@pytest.mark.skipif(os.name == "nt", reason="POSIX external socket proof")
def test_external_interactive_reconnect_replay_and_idempotent_submit():
    with tempfile.TemporaryDirectory() as directory:
        lock, endpoint = str(Path(directory)/"hub.lock"), str(Path(directory)/"hub.sock")
        process = _start(lock, endpoint)
        try:
            code, vscode = HubSocketClient(endpoint), HubSocketClient(endpoint)
            hello = code.request("h1", "handshake", client_id="code", schemas=["simplicio.hub-interactive/v1"])
            attached = code.request("a1", "attach", client_id="code", session_id="s1", epoch=hello["epoch"], cursor=0)
            assert attached["runtime_handle"]["id"] == attached["map_handle"]["id"] == "s1"
            first = code.request("op1", "submit", client_id="code", session_id="s1", job_id="job-1", metadata={})
            duplicate = vscode.request("op1", "submit", client_id="code", session_id="s1", job_id="job-1", metadata={})
            assert duplicate["replayed"] and duplicate["cursor"] == first["cursor"]
            conflict = vscode.request("op1", "submit", client_id="code", session_id="s1", job_id="other", metadata={})
            assert conflict["ok"] is False
            replay = vscode.request("a2", "attach", client_id="code", session_id="s1", epoch=hello["epoch"], cursor=0)
            assert [event["method"] for event in replay["events"]] == ["submit"]
            cancel = code.request("op2", "cancel", client_id="code", session_id="s1", job_id="job-1")
            assert cancel["job"]["state"] == "cancelled"
            assert vscode.request("op2", "cancel", client_id="code", session_id="s1", job_id="job-1")["replayed"]
            resumed = code.request("op3", "resume", client_id="code", session_id="s1", job_id="job-1")
            assert resumed["job"]["state"] == "queued"
            assert vscode.request("op3", "resume", client_id="code", session_id="s1", job_id="job-1")["replayed"]
        finally: _stop(process)
        process = _start(lock, endpoint)
        try:
            client = HubSocketClient(endpoint)
            hello = client.request("h2", "handshake", client_id="code", schemas=["simplicio.hub-interactive/v1"])
            replay = client.request("a3", "attach", client_id="code", session_id="s1", epoch=hello["epoch"], cursor=0)
            assert replay["events"][0]["request_id"] == "op1"
            assert client.request("op1", "submit", client_id="code", session_id="s1", job_id="job-1", metadata={})["replayed"]
            bad = client.request_raw(json.dumps({"schema":"simplicio.hub-ipc/v1","version":99,"request_id":"bad","method":"handshake","payload":{}}))
            assert bad["ok"] is False
        finally: _stop(process)
