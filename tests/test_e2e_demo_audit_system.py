import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "e2e_demo.py")


def _run(args, cwd):
    return subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True,
                          cwd=cwd, timeout=30, stdin=subprocess.DEVNULL)


def _event(hop, kind="measured", baseline=100, treatment=50):
    saved = baseline - treatment
    pct = round(100.0 * saved / baseline, 1) if baseline else 0.0
    return {
        "schema": "simplicio.savings-event/v1",
        "hop": hop,
        "measured_at": "2026-07-10T00:00:00Z",
        "tokens": {
            "baseline": baseline,
            "treatment": treatment,
            "saved": saved,
            "saved_pct": pct,
        },
        "proof": {
            "kind": kind,
            "tokenizer": "ceil(chars/4)",
            "methodology": "test fixture",
            "sources": ["fixture"],
        },
        "note": "fixture %s" % hop,
    }


def test_e2e_demo_audit_fails_when_any_hop_is_simulated(tmp_path):
    events = tmp_path / "events.jsonl"
    payloads = [
        _event("map"),
        _event("recall"),
        _event("edit", kind="simulated"),
        _event("verify"),
    ]
    events.write_text("".join(json.dumps(p) + "\n" for p in payloads), encoding="utf-8")
    r = _run(["audit", "--events", str(events), "--require-measured"], REPO)
    assert r.returncode == 2, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is False
    assert body["simulated"] == ["edit"]
    assert body["duplicates"] == []
    assert body["malformed"] == []


def test_e2e_demo_audit_passes_when_all_hops_are_measured(tmp_path):
    events = tmp_path / "events.jsonl"
    payloads = [
        _event("map"),
        _event("recall"),
        _event("edit"),
        _event("verify"),
    ]
    events.write_text("".join(json.dumps(p) + "\n" for p in payloads), encoding="utf-8")
    r = _run(["audit", "--events", str(events), "--require-measured"], REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is True
    assert body["simulated"] == []
    assert body["duplicates"] == []
    assert body["malformed"] == []


def test_e2e_demo_audit_fails_on_duplicate_hop(tmp_path):
    events = tmp_path / "events.jsonl"
    payloads = [_event("map"), _event("map"), _event("recall"), _event("edit"), _event("verify")]
    events.write_text("".join(json.dumps(p) + "\n" for p in payloads), encoding="utf-8")
    r = _run(["audit", "--events", str(events), "--require-measured"], REPO)
    assert r.returncode == 2, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is False
    assert body["duplicates"] == ["map"]


def test_e2e_demo_audit_fails_on_malformed_event(tmp_path):
    events = tmp_path / "events.jsonl"
    payloads = [_event("map"), _event("recall"), _event("edit"), _event("verify")]
    payloads[2]["tokens"]["saved"] = 999
    payloads[2]["proof"]["tokenizer"] = "wrong"
    events.write_text("".join(json.dumps(p) + "\n" for p in payloads), encoding="utf-8")
    r = _run(["audit", "--events", str(events), "--require-measured"], REPO)
    assert r.returncode == 2, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is False
    assert body["malformed"][0]["hop"] == "edit"
    assert "tokens.saved" in body["malformed"][0]["problems"]
    assert "proof.tokenizer" in body["malformed"][0]["problems"]
