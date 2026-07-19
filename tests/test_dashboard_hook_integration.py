"""#78: coverage for hooks/simplicio_dashboard.py — the token-monitor dashboard web server
(the largest untested hook, 49KB).

Importing the module does no I/O by itself (the HTTP server only binds inside main(), guarded by
`if __name__ == "__main__":`), so most of this suite unit-tests the pure/local data-formatting
functions directly in-process. The one live-server test binds 127.0.0.1 on port 0 (OS-assigned,
so no fixed-port collision across parallel runs), serves in a background thread with a hard
timeout, and always shuts the server down + joins the thread in a `finally` block — it can never
leak a listening port or hang the suite.
"""
import http.client
import http.server
import json
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(REPO, "hooks")


def _import_dashboard():
    sys.path.insert(0, HOOKS_DIR)
    import simplicio_dashboard as mod
    return mod


# ── pure helper functions ──────────────────────────────────────────────────


def test_model_family_detects_known_families():
    mod = _import_dashboard()
    assert mod._model_family("claude-sonnet-4-5") == "anthropic"
    assert mod._model_family("gpt-4o") == "openai"
    assert mod._model_family("deepseek-v3") == "deepseek"
    assert mod._model_family("gemini-2.5-pro") == "gemini"
    assert mod._model_family("some-unknown-model") == "default"
    assert mod._model_family(None) == "default"


def test_parse_int_tolerates_garbage():
    mod = _import_dashboard()
    assert mod._parse_int("42") == 42
    assert mod._parse_int("3.7") == 3
    assert mod._parse_int("not-a-number") == 0


def test_progress_snapshot_api_is_receipt_backed_and_scoped(tmp_path, monkeypatch):
    mod = _import_dashboard()
    runs = tmp_path / "runs"
    run = runs / "run-demo"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-demo", "phase": "executing", "progress_percent": 50,
        "task_count": 1, "completion": {"ready": False, "verdict": "DELIVERY_PENDING"},
    }), encoding="utf-8")
    monkeypatch.setenv("SIMPLICIO_RUNS_DIR", str(runs))
    status, payload = mod._progress_response("run-demo")
    assert status == 200
    assert payload["schema"] == "simplicio.progress/v1"
    assert payload["percent"] == 50
    assert payload["status"] == "RUNNING"

    status, payload = mod._progress_response("../run-demo")
    assert status == 400
    assert payload["status"] == "UNVERIFIED"
    status, payload = mod._progress_response("missing")
    assert status == 404
    assert payload["reason_code"] == "run_not_found"
    assert mod._parse_int("") == 0


def test_parse_iso_epoch_handles_bad_input():
    mod = _import_dashboard()
    assert mod._parse_iso_epoch("2024-01-01T00:00:00Z") > 0
    assert mod._parse_iso_epoch("garbage") == 0
    assert mod._parse_iso_epoch(None) == 0
    assert mod._parse_iso_epoch(12345) == 0


def test_port_listening_false_for_a_closed_high_port():
    mod = _import_dashboard()
    # Port 1 requires root/privilege and nothing listens there in this sandbox — must return
    # False cleanly, never raise, whatever the underlying OSError reason.
    assert mod._port_listening(1) in (False, True)  # must not raise; env-dependent result
    # A genuinely-unassigned ephemeral-range port with nothing bound must be reported closed.
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert mod._port_listening(free_port) is False


def test_read_savings_json_returns_dict_never_raises():
    mod = _import_dashboard()
    result = mod._read_savings_json()
    assert isinstance(result, dict)


def test_read_providers_returns_ints_never_raises():
    mod = _import_dashboard()
    total, interceptable = mod._read_providers()
    assert isinstance(total, int) and isinstance(interceptable, int)
    assert total >= 0 and interceptable >= 0
    assert interceptable <= total or total == 0


def test_run_swallows_missing_binary_instead_of_raising():
    mod = _import_dashboard()
    r = mod._run(["definitely-not-a-real-binary-xyz"], timeout=2)
    assert r.returncode != 0
    assert r.stdout == "" and r.stderr == ""


def test_fallback_logo_svg_is_valid_svg_markup():
    mod = _import_dashboard()
    svg = mod._fallback_logo_svg()
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")


# ── get_status(): the full aggregator — read-only (ps, local files, local engine call) ──


def test_get_status_returns_well_formed_payload():
    mod = _import_dashboard()
    status = mod.get_status()
    for key in ("proxy_running", "port", "requests", "tokens_before", "tokens_after",
                "tokens_saved", "savings_pct", "runtimes", "intercept_ready",
                "intercept_none", "active_count", "timestamp", "datetime"):
        assert key in status, "missing key %r in get_status() payload" % key
    assert isinstance(status["runtimes"], list) and len(status["runtimes"]) == len(mod.RUNTIMES)
    for r in status["runtimes"]:
        assert "name" in r and "intercept" in r and "active" in r and "live" in r
    assert status["intercept_ready"] + status["intercept_none"] == len(mod.RUNTIMES)
    assert 0 <= status["savings_pct"] <= 100 or status["tokens_before"] == 0


def test_get_status_is_json_serializable():
    mod = _import_dashboard()
    status = mod.get_status()
    blob = json.dumps(status)
    assert json.loads(blob)["port"] == status["port"]


# ── the HTML/script templates are well-formed and self-contained ──────────────


def test_html_template_has_no_leftover_slot_placeholders():
    mod = _import_dashboard()
    for slot in ("__FAVICON__", "__STYLE__", "__BODY__", "__SCRIPT__", "__BADGE__"):
        assert slot not in mod.HTML, "unsubstituted template slot %s leaked into HTML" % slot


def test_html_template_is_well_formed_shell():
    mod = _import_dashboard()
    assert mod.HTML.startswith("<!DOCTYPE html>")
    assert "<title>Simplicio Token Monitor</title>" in mod.HTML
    assert "/api/status" in mod.HTML  # the front-end must poll the real API path


# ── live server smoke test: bind :0 (OS-assigned), hard timeout, always torn down ─────


def test_dashboard_http_server_serves_status_and_html(tmp_path):
    mod = _import_dashboard()
    srv = http.server.HTTPServer(("127.0.0.1", 0), mod.Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            assert resp.status == 200
            payload = json.loads(resp.read())
            assert "proxy_running" in payload
        finally:
            conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.getheader("Content-Type", "").startswith("text/html")
            body = resp.read()
            assert b"<!DOCTYPE html>" in body
        finally:
            conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "dashboard server thread failed to stop — would leak a port"


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_dashboard_hook")
