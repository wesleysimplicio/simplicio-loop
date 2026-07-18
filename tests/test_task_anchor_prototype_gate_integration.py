"""#568 P0 slice: `scripts/task_anchor.py`'s done-gate (`cmd_gate`) also blocks "done" while a
work item has a TRACKED, unresolved Prototype-First flow (`simplicio_loop.prototype_gate`).

Fail-open by design: no tracked state -> gate behaves exactly as it did before this wiring
(covered by `tests/test_task_anchor_infra_gate_unit.py`, left untouched here). This file only
covers the NEW behavior: a tracked in_progress flow blocks even with 100% AC coverage, and a
resolved/rejected/blocked flow lets the AC-coverage gate decide on its own again.

Both the CLI subprocess (`task_anchor.py gate`) and this test's own direct calls into
`simplicio_loop.prototype_gate` are pointed at the SAME scratch state directory via
`SIMPLICIO_PROTOTYPE_STATE_DIR` -- set in-process with `monkeypatch` for the direct calls, and
forwarded into the subprocess environment for the CLI calls.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from simplicio_loop import prototype_gate as pg

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "task_anchor.py"


def _cli(anchor_path, state_dir, *args):
    env = dict(os.environ)
    env["SIMPLICIO_ANCHOR_FILE"] = str(anchor_path)
    env["SIMPLICIO_PROTOTYPE_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO, text=True, capture_output=True, env=env,
    )


def _fully_verified_anchor(tmp_path, state_dir, item="wi-568-gate"):
    anchor = tmp_path / "anchor.json"
    r = _cli(anchor, state_dir, "set", "--item", item, "--goal", "g", "--ac", "Unit tests pass")
    assert r.returncode == 0
    r = _cli(anchor, state_dir, "mark", "--id", "AC1", "--status", "done",
            "--evidence", "tests/test_x.py::test_y PASSED")
    assert r.returncode == 0
    return anchor


def test_gate_is_ready_with_full_ac_coverage_and_no_tracked_prototype_flow(tmp_path):
    state_dir = tmp_path / "proto-state-a"
    anchor = _fully_verified_anchor(tmp_path, state_dir)
    r = _cli(anchor, state_dir, "gate", "--json")
    payload = json.loads(r.stdout)
    assert payload["ready"] is True
    assert payload["prototype_gate"] == {
        "tracked": False, "ready": True,
        "reason": "no prototype flow tracked for this item",
    }


def test_gate_blocks_when_prototype_flow_is_in_progress_despite_full_ac_coverage(tmp_path, monkeypatch):
    state_dir = tmp_path / "proto-state-b"
    item = "wi-568-inprogress"
    anchor = _fully_verified_anchor(tmp_path, state_dir, item=item)

    monkeypatch.setenv("SIMPLICIO_PROTOTYPE_STATE_DIR", str(state_dir))
    plan = pg.build_plan(work_item_id=item, goal="g", prototype_type="schema", source_sha="s",
                         level="P0")
    state = pg.init_state(work_item_id=item, plan=plan)
    pg.save_state(state, repo=str(state_dir))

    r = _cli(anchor, state_dir, "gate", "--json")
    payload = json.loads(r.stdout)
    assert payload["ready"] is False
    assert payload["prototype_gate"]["tracked"] is True
    assert payload["prototype_gate"]["ready"] is False
    assert r.returncode == 0  # --exit-code not passed here

    r2 = _cli(anchor, state_dir, "gate", "--exit-code")
    assert r2.returncode == 12
    assert "prototype gate" in r2.stdout


def test_gate_unblocks_once_prototype_flow_resolves(tmp_path, monkeypatch):
    state_dir = tmp_path / "proto-state-c"
    item = "wi-568-resolved"
    anchor = _fully_verified_anchor(tmp_path, state_dir, item=item)

    monkeypatch.setenv("SIMPLICIO_PROTOTYPE_STATE_DIR", str(state_dir))
    plan = pg.build_plan(work_item_id=item, goal="g", prototype_type="schema", source_sha="s",
                         level="FULL")
    candidate = pg.build_candidate(plan=plan, candidate_id="c1", strategy="direct", agent_id="a1",
                                   artifact_hash="h1")
    decision = pg.build_decision(plan=plan, candidate_hash=candidate["candidate_hash"],
                                 decision="ACCEPT")
    state = pg.init_state(work_item_id=item, plan=plan)
    state = pg.apply_decision(state, plan=plan, decision=decision,
                              candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "resolved"
    pg.save_state(state, repo=str(state_dir))

    r = _cli(anchor, state_dir, "gate", "--json")
    payload = json.loads(r.stdout)
    assert payload["ready"] is True
    assert payload["prototype_gate"]["tracked"] is True
    assert payload["prototype_gate"]["ready"] is True


def test_gate_treats_a_recorded_reject_as_terminal_not_a_dangling_block(tmp_path, monkeypatch):
    state_dir = tmp_path / "proto-state-d"
    item = "wi-568-rejected"
    anchor = _fully_verified_anchor(tmp_path, state_dir, item=item)

    monkeypatch.setenv("SIMPLICIO_PROTOTYPE_STATE_DIR", str(state_dir))
    plan = pg.build_plan(work_item_id=item, goal="g", prototype_type="schema", source_sha="s",
                         level="P0")
    candidate = pg.build_candidate(plan=plan, candidate_id="c1", strategy="direct", agent_id="a1",
                                   artifact_hash="h1")
    decision = pg.build_decision(plan=plan, candidate_hash=candidate["candidate_hash"],
                                 decision="REJECT", reason="hypothesis disproven")
    state = pg.init_state(work_item_id=item, plan=plan)
    state = pg.apply_decision(state, plan=plan, decision=decision,
                              candidate_hash=candidate["candidate_hash"])
    assert state["status"] == "rejected"
    pg.save_state(state, repo=str(state_dir))

    r = _cli(anchor, state_dir, "gate", "--json")
    payload = json.loads(r.stdout)
    assert payload["ready"] is True
    assert payload["prototype_gate"]["status"] == "rejected"
