import json

from simplicio_loop.map_service_cli import run


def test_build_verify_and_status_use_standalone_receipts(tmp_path, capsys):
    assert run("build", repo=str(tmp_path), tree_hash="sha", files=["a.py"], as_json=True) == 0
    build = json.loads(capsys.readouterr().out)
    assert build["status"] == "READY"
    assert build["fallback"] is True
    assert run("verify", repo=str(tmp_path), as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY"
    assert run("status", repo=str(tmp_path), as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "status"


def test_gc_is_safe_noop_without_hub_store(tmp_path, capsys):
    assert run("gc", repo=str(tmp_path), as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == []
    assert payload["reason_code"] == "standalone_no_store"
