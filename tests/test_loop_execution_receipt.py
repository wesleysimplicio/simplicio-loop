from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from simplicio_loop import loop_execution_receipt as receipt_mod


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    run = repo / ".simplicio" / "loop-runs" / "run-1"
    loop = run / "loop"
    loop.mkdir(parents=True)
    _write_json(run / "manifest.json", {"run_id": "run-1"})
    _write_json(run / "stack-lock.json", {
        "components": [
            {"name": "simplicio-mapper", "version": "0.26.11", "executable": "mapper"},
            {"name": "simplicio-cli", "version": "0.18.6", "executable": "dev-cli"},
            {"name": "simplicio-fast", "version": "2.0.23", "executable": "fast", "available": True},
            {"name": "simplicio-runtime", "version": "3.5.7", "executable": "runtime"},
        ]
    })
    _write_json(run / "mapper-preflight.json", {"version": "0.26.11"})
    _write_json(run / "operator-preflight.json", {"version": "0.18.6"})
    _write_json(run / "mapper-context.json", {"run_id": "run-1", "status": "ok"})
    _write_json(run / "operator-receipt.json", {"run_id": "run-1", "returncode": 0, "execution_state": "executed"})
    (loop / "scratchpad.md").write_text(
        "---\niteration: 1\nmax_iterations: 2\ncompletion_promise: null\n"
        "evidence_required: true\nmode: converge\nstarted_at: 2026-08-03T00:00:00Z\n---\ngoal\n",
        encoding="utf-8",
    )
    (loop / "journal.jsonl").write_text(
        '{"iteration":1,"action":"run","hypothesis":"works","gate":"pass",'
        '"fingerprint":"","note":"ok","ts":"2026-08-03T00:00:01Z"}\n',
        encoding="utf-8",
    )
    _write_json(loop / "anchor.json", {
        "item": "run-1", "goal": "works", "goal_fp": "abc", "frozen_at": "2026-08-03T00:00:00Z",
        "criteria": [{"id": "AC1", "text": "works", "status": "done"}],
    })
    _write_json(loop / "watcher_challenge.json", {
        "challenge": "nonce", "iteration": 1, "goal_fp": "abc", "written_at": "2026-08-03T00:00:01Z",
    })
    _write_json(loop / "watcher_state.json", {
        "match": True, "status": "MEASURED", "challenge": "nonce", "goal_fp": "abc",
        "checked_at": "2026-08-03T00:00:02Z",
    })
    return repo, run


def test_publish_creates_runtime_bound_snapshot(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    result = receipt_mod.publish_loop_execution_receipt(
        repo=repo, run_dir=run, manifest={"run_id": "run-1"}
    )

    assert result["status"] == "VERIFIED"
    envelope = json.loads((repo / ".simplicio" / "loop-execution.json").read_text(encoding="utf-8"))
    assert envelope["chain"] == receipt_mod.CHAIN
    assert envelope["result"] == {"run_id": "run-1", "status": "VERIFIED", "verified": True}
    assert envelope["fast"]["version"] == "2.0.23"
    bundle = run / "runtime-loop-execution"
    for entry in envelope["artifacts"].values():
        copied = bundle / entry["path"]
        assert copied.is_file()
        assert entry["sha256"] == receipt_mod._sha256(copied)
        assert "source" not in entry
    assert json.loads((bundle / "mapper.json").read_text(encoding="utf-8"))["run_id"] == "run-1"
    assert json.loads((bundle / "dev-cli.json").read_text(encoding="utf-8"))["returncode"] == 0


def test_publish_skips_non_git_legacy_fixture(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    monkeypatch.setattr(
        receipt_mod,
        "_git_commit",
        lambda _repo: (_ for _ in ()).throw(receipt_mod.LoopExecutionReceiptError("not a git repo")),
    )

    result = receipt_mod.publish_loop_execution_receipt(
        repo=repo, run_dir=run, manifest={"run_id": "run-1"}
    )

    assert result == {
        "status": "SKIPPED",
        "reason": "repository_not_git",
        "detail": "not a git repo",
    }
    assert not (repo / ".simplicio" / "loop-execution.json").exists()


def test_publish_rejects_missing_state_artifact(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    (run / "loop" / "watcher_state.json").unlink()
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="watcher_state.json"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )


def test_publish_rejects_manifest_run_id_mismatch(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="does not match"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "other-run"}
        )


def test_publish_rejects_run_directory_outside_workspace(tmp_path):
    repo = tmp_path / "repo"
    run = tmp_path / "outside" / "run-1"
    repo.mkdir()

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="escapes"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )


def test_publish_rejects_symlinked_source_artifact(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    outside = tmp_path / "outside-watcher-state.json"
    outside.write_text("{}", encoding="utf-8")
    target = run / "loop" / "watcher_state.json"
    target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="escapes|symlink"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )


def test_publish_rejects_fallback_component(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    stack_lock = json.loads((run / "stack-lock.json").read_text(encoding="utf-8"))
    stack_lock["components"][0]["fallback"] = True
    _write_json(run / "stack-lock.json", stack_lock)
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="fallback"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )


def test_publish_rejects_existing_bundle_without_overwrite(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    (run / "runtime-loop-execution").mkdir()
    (run / "runtime-loop-execution" / "sentinel").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="already exists"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )
    assert (run / "runtime-loop-execution" / "sentinel").read_text(encoding="utf-8") == "keep"


def test_publish_removes_partial_bundle_when_root_write_fails(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)
    monkeypatch.setattr(
        receipt_mod,
        "_atomic_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            receipt_mod.LoopExecutionReceiptError("root write failed")
        ),
    )

    with pytest.raises(receipt_mod.LoopExecutionReceiptError, match="root write failed"):
        receipt_mod.publish_loop_execution_receipt(
            repo=repo, run_dir=run, manifest={"run_id": "run-1"}
        )
    assert not (run / "runtime-loop-execution").exists()


def test_receipt_schema_declares_stable_runtime_chain():
    schema_path = Path(__file__).parents[1] / "contracts" / "loop-execution" / "v1" / "receipt.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == receipt_mod.SCHEMA
    assert schema["properties"]["chain"]["const"] == receipt_mod.CHAIN
