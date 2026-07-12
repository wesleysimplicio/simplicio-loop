import json
from pathlib import Path

from simplicio_loop.cli import main


def _seed(tmp_path: Path):
    loop = tmp_path / "loop"
    run = tmp_path / "run"
    loop.mkdir(); run.mkdir()
    (loop / "scratchpad.md").write_text(
        '---\ncompletion_promise: "DONE"\n---\ngoal\n', encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({"challenge": "c", "written_at": "2026-07-10T00:00:00Z"}), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({"match": True, "status": "MEASURED", "challenge": "c", "checked_at": "2026-07-10T00:00:01Z"}), encoding="utf-8")
    files = {
        "manifest.json": {"delivery_target": "verified"},
        "task-contract.json": {"schema": "simplicio.task-contract-collection/v1"},
        "mapper-context.json": {"handoff": {}},
        "operator-receipt.json": {"schema": "simplicio.operator-receipt/v0"},
        "evidence-receipt.json": {"schema": "simplicio.evidence-receipt/v1", "status": "VERIFIED", "criteria": [{"id": "AC1", "verification_state": "verified"}]},
        "delivery-receipt.json": {"schema": "simplicio.delivery-receipt/v1", "target": "verified", "current_state": "verified", "ready": True, "source_kind": "local", "source_payload": {"evidence_receipt": "evidence-receipt.json", "criteria_verified": 1}},
    }
    for name, payload in files.items():
        (run / name).write_text(json.dumps(payload), encoding="utf-8")
    return loop, run


def test_oracle_cli_emits_adapter_matrix_and_receipt(tmp_path, capsys):
    loop, run = _seed(tmp_path)
    rc = main(["oracle", "--loop-dir", str(loop), "--run-dir", str(run),
               "--response-text", "<promise>DONE</promise>", "--write-receipt"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "simplicio.completion-oracle-matrix/v1"
    assert payload["parity"] is True
    assert len(payload["adapters"]) == 7
    assert Path(payload["receipt_path"]).exists()


def test_oracle_cli_fail_closed_on_missing_state(tmp_path, capsys):
    loop = tmp_path / "loop"; loop.mkdir()
    rc = main(["oracle", "--loop-dir", str(loop), "--run-dir", str(tmp_path / "run"),
               "--response-text", "<promise>DONE</promise>"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["parity"] is True
    assert payload["signature"][1] == "DELIVERY_PENDING"
