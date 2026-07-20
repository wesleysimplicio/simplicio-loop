from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.merge_queue_live_probe import probe  # noqa: E402


class _LocalFixtureHandler(BaseHTTPRequestHandler):
    payload: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/local-test-only/merge-queue-live":
            self.send_response(404)
            self.end_headers()
            return
        wire = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)


@pytest.fixture
def local_http_fixture() -> tuple[ThreadingHTTPServer, str]:
    payload = {
        "fixture_mode": "LOCAL_TEST_ONLY_NOT_PRODUCTION",
        "merge_queue": {
            "receipt_sha": "sha-merge-123",
            "status": "accepted",
            "branch": "simplicio/run-ac7/WI-1",
            "worktree_path": "/tmp/local-fixture/WI-1",
            "tree_sha": "tree-abc-123",
        },
        "evidence_gate": {"ready": True, "status": "MEASURED"},
        "board": {"status": "COMPLETE", "summary": {"completion_percent": 100, "fronts_converged": True}},
    }
    _LocalFixtureHandler.payload = payload
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}/local-test-only/merge-queue-live"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_receipt(path: Path, *, branch: str = "simplicio/run-ac7/WI-1",
                   worktree_path: str = "/tmp/local-fixture/WI-1",
                   tree_sha: str = "tree-abc-123",
                   status: str = "accepted",
                   evidence_ready: bool = True,
                   evidence_status: str = "MEASURED",
                   board_status: str = "COMPLETE",
                   completion_percent: int = 100,
                   fronts_converged: bool = True) -> Path:
    payload = {
        "merge_queue": {
            "receipt_sha": "sha-merge-123",
            "status": status,
            "branch": branch,
            "worktree_path": worktree_path,
            "tree_sha": tree_sha,
        },
        "evidence_gate": {"ready": evidence_ready, "status": evidence_status},
        "board": {"status": board_status, "summary": {"completion_percent": completion_percent, "fronts_converged": fronts_converged}},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_probe_passes_with_matching_local_fixture(tmp_path, local_http_fixture):
    _, endpoint = local_http_fixture
    receipt = _write_receipt(tmp_path / "external-receipt.json")

    result = probe(endpoint=endpoint, receipt=str(receipt))

    assert result["tag"] == "MEASURED"
    assert result["verdict"] == "PASS"
    assert all(result["acceptance"].values()), result["acceptance"]
    assert result["sources"]["endpoint"].endswith("/local-test-only/merge-queue-live")
    assert result["live"]["branch"] == "simplicio/run-ac7/WI-1"


@pytest.mark.parametrize(
    ("endpoint", "receipt"),
    [
        ("", "configured"),
        ("configured", ""),
    ],
)
def test_probe_is_fail_closed_without_endpoint_or_receipt(tmp_path, local_http_fixture, endpoint, receipt):
    _, live_endpoint = local_http_fixture
    receipt_path = _write_receipt(tmp_path / "external-receipt.json")

    result = probe(
        endpoint=live_endpoint if endpoint == "configured" else "",
        receipt=str(receipt_path) if receipt == "configured" else "",
    )

    assert result["tag"] == "UNVERIFIED"
    assert result["verdict"] == "FAIL"
    assert result["fail_closed"] is True
    assert not all(result["acceptance"].values())


def test_probe_fails_when_live_state_diverges_from_receipt(tmp_path, local_http_fixture):
    _, endpoint = local_http_fixture
    receipt = _write_receipt(
        tmp_path / "external-receipt.json",
        branch="simplicio/run-ac7/WI-OTHER",
        tree_sha="tree-other",
    )

    result = probe(endpoint=endpoint, receipt=str(receipt))

    assert result["tag"] == "UNVERIFIED"
    assert result["verdict"] == "FAIL"
    assert result["acceptance"]["branch_matches"] is False
    assert result["acceptance"]["tree_sha_matches"] is False
    assert "branch_mismatch" in result["reason_codes"]
    assert "tree_sha_mismatch" in result["reason_codes"]
