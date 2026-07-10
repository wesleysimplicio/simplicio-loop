"""Tests for the Phase-0 backlog worker.

The new worker must share the anchor AC lint helper (same vague-AC refusal and optional strict
short-AC rule) and render a deterministic markdown table for PR evidence.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKLOG = os.path.join(REPO, "scripts", "task_backlog.py")


def _run(args, cwd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, BACKLOG] + args, capture_output=True, text=True,
                          cwd=cwd, env=full_env, stdin=subprocess.DEVNULL)


def test_backlog_init_rejects_vague_ac_by_default(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([{"id": "T1", "goal": "One goal", "acs": ["works"]}]),
                         encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "vague acceptance criterion refused" in r.stdout, r.stdout


def test_backlog_init_strict_rejects_short_ac(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([{"id": "T1", "goal": "One goal", "acs": ["one ac"]}]),
                         encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file), "--lint"],
             str(tmp_path), env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "strict lint refused short acceptance criterion" in r.stdout, r.stdout


def test_backlog_checklist_renders_table(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "Goal with | pipe", "acs": ["A real criterion"]},
        {"id": "T2", "goal": "Other goal", "acs": ["Another real criterion"]},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    c = _run(["checklist"], str(tmp_path), env)
    assert c.returncode == 0, c.stdout + c.stderr
    assert "Body of work" in c.stdout, c.stdout
    assert r"\|" in c.stdout, c.stdout


def test_backlog_next_respects_dependencies_and_priority(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "Base layer", "acs": ["A real criterion"], "priority": 20},
        {"id": "T2", "goal": "Depends on T1", "acs": ["Another real criterion"], "depends_on": ["T1"], "priority": 10},
        {"id": "T3", "goal": "Independent high priority", "acs": ["Third real criterion"], "priority": 5},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    n1 = _run(["next"], str(tmp_path), env)
    assert n1.returncode == 0, n1.stdout + n1.stderr
    assert n1.stdout.startswith("T3\t"), n1.stdout
    n2 = _run(["next"], str(tmp_path), env)
    assert n2.returncode == 0, n2.stdout + n2.stderr
    assert n2.stdout.startswith("T1\t"), n2.stdout
    n3 = _run(["next"], str(tmp_path), env)
    assert n3.returncode == 0, n3.stdout + n3.stderr
    assert "no ready items" in n3.stdout, n3.stdout


def test_backlog_init_rejects_dependency_cycle(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "A", "acs": ["A real criterion"], "depends_on": ["T2"]},
        {"id": "T2", "goal": "B", "acs": ["Another real criterion"], "depends_on": ["T1"]},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "dependency cycle detected" in r.stdout, r.stdout


def test_backlog_block_records_reason_code(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "One goal", "acs": ["A real criterion"]},
    ]), encoding="utf-8")
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    b = _run(["block", "--item", "T1", "--reason", "waiting external auth", "--code", "auth-missing"],
             str(tmp_path), env)
    assert b.returncode == 0, b.stdout + b.stderr
    body = backlog_path.read_text(encoding="utf-8")
    assert '"reason_code": "auth-missing"' in body, body


def test_backlog_init_accepts_multi_task_markdown_without_manual_json(tmp_path):
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        """Sistema: PLANES
Funcionalidade: Tela de Modelagem — Ordenação de linhas
Tipo: Evolução

COMO analista do ONS,
QUERO que as linhas da tela de modelagem sejam ordenadas por tipo
PARA facilitar a análise.

1. Critérios de Aceite

Cenário 1: Estrutural aparece primeiro
  Dado que a usina possui linhas do tipo estrutural e temporal
  Quando a tela de modelagem for exibida
  Então a linha do tipo estrutural deve aparecer primeiro [RN01]

2. Regras de Negócio

RN01 – Dentro de cada usina, a linha do tipo estrutural deve sempre aparecer primeiro.

Sistema: PLANES
Funcionalidade: Tela de Modelagem — Ordem alfabética por usina
Tipo: Evolução

COMO analista do ONS,
QUERO que as usinas sejam exibidas em ordem alfabética
PARA localizar os estudos mais rápido.

1. Critérios de Aceite

Cenário 1: Usinas em ordem alfabética
  Dado que existem múltiplas usinas na tela de modelagem
  Quando a tela for exibida
  Então as usinas devem estar em ordem alfabética [RN03]

2. Regras de Negócio

RN03 – As usinas devem ser exibidas em ordem alfabética.
""",
        encoding="utf-8",
    )
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--task-file", str(task_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "frozen 2 item(s)" in r.stdout, r.stdout
    body = backlog_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert '"id": "T1"' in body, body
    assert '"id": "T2"' in body, body
    item_records = [record for record in records if record.get("kind") == "item"]
    assert item_records[0]["plan_files"] == [str(task_file)]
    assert item_records[1]["plan_files"] == [str(task_file)]
    assert os.path.exists(item_records[0]["run_dir"])
    assert os.path.exists(os.path.join(item_records[0]["run_dir"], "task-contract.json"))
    assert os.path.exists(os.path.join(item_records[0]["run_dir"], "loop", "anchor.json"))
    assert os.path.exists(os.path.join(item_records[0]["run_dir"], "loop", "journal.jsonl"))
    assert os.path.exists(os.path.join(item_records[0]["run_dir"], "evidence-receipt.json"))
    assert os.path.exists(os.path.join(item_records[0]["run_dir"], "delivery-receipt.json"))
    assert "Estrutural aparece primeiro" in body, body
    assert "Usinas em ordem alfabética" in body, body


def test_backlog_next_reuses_claim_for_same_worker_and_heartbeat_extends_lease(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "One goal", "acs": ["A real criterion"]},
        {"id": "T2", "goal": "Two goal", "acs": ["Another real criterion"]},
    ]), encoding="utf-8")
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    n1 = _run(["next", "--worker", "w1", "--lease-ttl", "120"], str(tmp_path), env)
    assert n1.returncode == 0, n1.stdout + n1.stderr
    assert n1.stdout.startswith("T1\t"), n1.stdout
    before = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    item_before = next(record for record in before if record.get("kind") == "item" and record.get("id") == "T1")
    hb = _run(["heartbeat", "--item", "T1", "--worker", "w1", "--lease-ttl", "120"], str(tmp_path), env)
    assert hb.returncode == 0, hb.stdout + hb.stderr
    assert "heartbeat T1" in hb.stdout, hb.stdout
    n2 = _run(["next", "--worker", "w1", "--lease-ttl", "120"], str(tmp_path), env)
    assert n2.returncode == 0, n2.stdout + n2.stderr
    assert n2.stdout.startswith("T1\t"), n2.stdout
    after = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    item_after = next(record for record in after if record.get("kind") == "item" and record.get("id") == "T1")
    assert item_after["lease"]["worker"] == "w1"
    assert item_after["lease"]["expires_at"] >= item_before["lease"]["expires_at"]


def test_backlog_next_reclaims_stale_lease_for_other_worker(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "One goal", "acs": ["A real criterion"]},
        {"id": "T2", "goal": "Two goal", "acs": ["Another real criterion"]},
    ]), encoding="utf-8")
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    n1 = _run(["next", "--worker", "w1", "--lease-ttl", "1"], str(tmp_path), env)
    assert n1.returncode == 0, n1.stdout + n1.stderr
    assert n1.stdout.startswith("T1\t"), n1.stdout
    body = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    items = [record for record in body if record.get("kind") == "item"]
    first = next(item for item in items if item["id"] == "T1")
    first["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    backlog_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in body) + "\n",
                            encoding="utf-8")
    n2 = _run(["next", "--worker", "w2", "--lease-ttl", "120"], str(tmp_path), env)
    assert n2.returncode == 0, n2.stdout + n2.stderr
    assert n2.stdout.startswith("T1\t"), n2.stdout
    body2 = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    first2 = next(record for record in body2 if record.get("kind") == "item" and record.get("id") == "T1")
    assert first2["lease"]["worker"] == "w2"
    assert first2["status"] == "claimed"


def test_backlog_next_serializes_same_plan_file_conflicts(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "Edit shared module", "acs": ["A real criterion"], "priority": 10,
         "plan_files": ["src/shared.py"]},
        {"id": "T2", "goal": "Also edit shared module", "acs": ["Another real criterion"], "priority": 20,
         "plan_files": ["src/shared.py"]},
        {"id": "T3", "goal": "Independent file", "acs": ["Third real criterion"], "priority": 30,
         "plan_files": ["src/other.py"]},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr

    first = _run(["next", "--worker", "w1", "--lease-ttl", "120"], str(tmp_path), env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.startswith("T1\t"), first.stdout

    second = _run(["next", "--worker", "w2", "--lease-ttl", "120"], str(tmp_path), env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.startswith("T3\t"), second.stdout

    third = _run(["next", "--worker", "w3", "--lease-ttl", "120"], str(tmp_path), env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "no ready items" in third.stdout, third.stdout


def test_backlog_next_allows_parallel_claim_for_independent_plan_files(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "Edit file A", "acs": ["A real criterion"], "priority": 10,
         "plan_files": ["src/a.py"]},
        {"id": "T2", "goal": "Edit file B", "acs": ["Another real criterion"], "priority": 20,
         "plan_files": ["src/b.py"]},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr

    first = _run(["next", "--worker", "w1", "--lease-ttl", "120"], str(tmp_path), env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.startswith("T1\t"), first.stdout

    second = _run(["next", "--worker", "w2", "--lease-ttl", "120"], str(tmp_path), env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.startswith("T2\t"), second.stdout


def test_backlog_fail_moves_to_dead_letter_after_distinct_failures(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "One goal", "acs": ["A real criterion"]},
    ]), encoding="utf-8")
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    claimed = _run(["next", "--worker", "w1"], str(tmp_path), env)
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    f1 = _run(["fail", "--item", "T1", "--worker", "w1", "--reason", "test failed", "--code", "test-red",
               "--fingerprint", "fp-1", "--max-failures", "3"], str(tmp_path), env)
    assert f1.returncode == 0, f1.stdout + f1.stderr
    f2 = _run(["fail", "--item", "T1", "--reason", "lint failed", "--code", "lint-red",
               "--fingerprint", "fp-2", "--max-failures", "3"], str(tmp_path), env)
    assert f2.returncode == 0, f2.stdout + f2.stderr
    f3 = _run(["fail", "--item", "T1", "--reason", "review failed", "--code", "review-red",
               "--fingerprint", "fp-3", "--max-failures", "3"], str(tmp_path), env)
    assert f3.returncode == 0, f3.stdout + f3.stderr
    assert "dead-letter T1" in f3.stdout, f3.stdout
    body = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    item = next(record for record in body if record.get("kind") == "item" and record.get("id") == "T1")
    assert item["status"] == "dead-letter"
    assert len(item["failures"]) == 3
    assert item["reason_code"] == "dead-letter"


def test_backlog_status_shows_dependency_chain_and_lease(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "Base layer", "acs": ["A real criterion"]},
        {"id": "T2", "goal": "Depends on base", "acs": ["Another real criterion"], "depends_on": ["T1"]},
    ]), encoding="utf-8")
    env = {"SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl")}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    c = _run(["next", "--worker", "worker-a", "--lease-ttl", "120"], str(tmp_path), env)
    assert c.returncode == 0, c.stdout + c.stderr
    s = _run(["status"], str(tmp_path), env)
    assert s.returncode == 0, s.stdout + s.stderr
    assert "claimed: 1" in s.stdout, s.stdout
    assert "blocked: 1" in s.stdout, s.stdout
    assert "worker-a" in s.stdout, s.stdout
    assert "T2 <- T1" in s.stdout, s.stdout


def test_backlog_invalidates_item_when_source_file_changes(tmp_path):
    task_file = tmp_path / "tasks.md"
    task_file.write_text(
        """Sistema: PLANES
Funcionalidade: Ordenacao
Tipo: Evolução

COMO analista
QUERO ordenar
PARA analisar

1. Critérios de Aceite

Cenário 1: Ordem
  Dado que existe uma lista
  Quando a tela for exibida
  Então a lista deve respeitar a regra [RN01]

2. Regras de Negócio

RN01 – Regra inicial.
""",
        encoding="utf-8",
    )
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--task-file", str(task_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    task_file.write_text(task_file.read_text(encoding="utf-8") + "\nRN02 – Regra nova.\n", encoding="utf-8")
    s = _run(["status"], str(tmp_path), env)
    assert s.returncode == 0, s.stdout + s.stderr
    assert "invalidated: 1" in s.stdout, s.stdout
    body = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    item = next(record for record in body if record.get("kind") == "item")
    assert item["status"] == "invalidated"
    assert item["reason_code"] == "source-changed"
    assert not os.path.exists(os.path.join(item["run_dir"], "evidence-receipt.json"))
    state = json.loads(open(os.path.join(item["run_dir"], "state.json"), encoding="utf-8").read())
    assert state["phase"] == "awaiting_refresh"


def test_backlog_poll_drains_after_k_empty_polls_with_zero_workers(tmp_path):
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "One goal", "acs": ["A real criterion"]},
    ]), encoding="utf-8")
    anchor_path = tmp_path / "anchor.json"
    anchor_path.write_text(json.dumps({
        "goal_fp": "placeholder",
        "criteria": [{"id": "AC1", "status": "done", "evidence": "shot.png"}]
    }), encoding="utf-8")
    backlog_path = tmp_path / "backlog.jsonl"
    env = {"SIMPLICIO_BACKLOG_FILE": str(backlog_path)}
    r = _run(["init", "--goal", "Drain Phase 0", "--item-file", str(item_file)], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    records = [json.loads(line) for line in backlog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    item = next(record for record in records if record.get("kind") == "item")
    anchor_path.write_text(json.dumps({
        "goal_fp": item["goal_fp"],
        "criteria": [{"id": "AC1", "status": "done", "evidence": "shot.png"}]
    }), encoding="utf-8")
    done = _run(["done", "--item", "T1", "--anchor", str(anchor_path)], str(tmp_path), env)
    assert done.returncode == 0, done.stdout + done.stderr
    p1 = _run(["poll", "--empty-polls", "2"], str(tmp_path), env)
    assert p1.returncode == 0, p1.stdout + p1.stderr
    assert "empty 1/2" in p1.stdout, p1.stdout
    p2 = _run(["poll", "--empty-polls", "2"], str(tmp_path), env)
    assert p2.returncode == 0, p2.stdout + p2.stderr
    assert "drained" in p2.stdout, p2.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_task_backlog")
