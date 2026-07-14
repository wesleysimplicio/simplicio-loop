import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from simplicio_loop import runner as runner_mod
from simplicio_loop.oracle import persist_completion_receipt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = [sys.executable, "-m", "simplicio_loop.cli"]

TASK = """Sistema: PLANES
Funcionalidade: Tela de Modelagem — Ordenação de linhas
Tipo: Evolução

COMO analista do ONS,
QUERO organizar as linhas
PARA melhorar a análise

1. Critérios de Aceite

Cenário 1: Estrutural aparece primeiro
  Dado que existe uma linha estrutural
  Quando a tela for exibida
  Então a linha estrutural aparece primeiro [RN01]

2. Regras de Negócio

RN01 – Estrutural sempre primeiro.
"""


def _run(cmd, cwd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=30,
                          stdin=subprocess.DEVNULL, env=full_env)


def _arm_result(repo, task, *, env=None, delivery="verified", max_iterations=12):
    """Exercise the explicit preparation API for tests that need an armed intermediate run."""
    with patch.dict(os.environ, env or {}, clear=False):
        payload = runner_mod.arm_run(
            str(repo), str(task), delivery, max_iterations,
        )
    return subprocess.CompletedProcess(
        args=["arm_run"], returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False), stderr="",
    )


def test_repo_state_equivalent_ignores_dirty_status_noise_when_tree_is_stable():
    before = {
        "head": "abc123",
        "dirty_status_hash": "before",
        "tree_hash": "tree-1",
    }
    after = {
        "head": "abc123",
        "dirty_status_hash": "after",
        "tree_hash": "tree-1",
    }

    assert runner_mod._repo_state_equivalent(before, after) is True
    assert runner_mod._repo_state_equivalent(before, {**after, "tree_hash": "tree-2"}) is False
    assert runner_mod._repo_state_equivalent(before, {**after, "head": "def456"}) is False


def test_operator_env_defaults_to_codex_gpt54_medium(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_MODEL", raising=False)
    monkeypatch.delenv("SIMPLICIO_CODEX_EFFORT", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_OPERATOR_MODEL", raising=False)
    monkeypatch.delenv("SIMPLICIO_LOOP_OPERATOR_EFFORT", raising=False)

    env = runner_mod._operator_env()

    assert env["SIMPLICIO_MODEL"] == "codex-cli/gpt-5.4"
    assert env["SIMPLICIO_CODEX_EFFORT"] == "medium"


def test_operator_env_preserves_explicit_outer_configuration(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_MODEL", "codex-cli/custom-model")
    monkeypatch.setenv("SIMPLICIO_CODEX_EFFORT", "high")
    monkeypatch.setenv("SIMPLICIO_LOOP_OPERATOR_MODEL", "codex-cli/gpt-5.4")
    monkeypatch.setenv("SIMPLICIO_LOOP_OPERATOR_EFFORT", "medium")

    env = runner_mod._operator_env()

    assert env["SIMPLICIO_MODEL"] == "codex-cli/custom-model"
    assert env["SIMPLICIO_CODEX_EFFORT"] == "high"


def test_mapper_preflight_requires_receipt_contract_minimum(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    run_root = repo / "run"
    repo.mkdir()
    run_root.mkdir()
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON", json.dumps({
        "version_stdout": "simplicio-mapper 0.18.9",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
    }))

    with pytest.raises(RuntimeError, match="below minimum version"):
        runner_mod._preflight_mapper(repo, run_root)

    receipt = json.loads((run_root / "mapper-preflight.json").read_text(encoding="utf-8"))
    assert receipt["min_version"] == "0.19.0"
    assert receipt["version_ok"] is False


def test_operator_timeout_defaults_and_override(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_OPERATOR_TIMEOUT_SEC", raising=False)
    assert runner_mod._operator_timeout("dry_run") == 60
    assert runner_mod._operator_timeout("execute") == 600

    monkeypatch.setenv("SIMPLICIO_LOOP_OPERATOR_TIMEOUT_SEC", "450")
    assert runner_mod._operator_timeout("dry_run") == 450
    assert runner_mod._operator_timeout("execute") == 450


def test_operator_env_promotes_loop_test_cmd_when_test_cmd_missing(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_TEST_CMD", raising=False)
    monkeypatch.setenv("SIMPLICIO_LOOP_TEST_CMD", "python -m pytest tests/python/test_task_spec.py -q")

    env = runner_mod._operator_env()

    assert env["SIMPLICIO_TEST_CMD"] == "python -m pytest tests/python/test_task_spec.py -q"


def test_devcli_cmd_prefers_repo_checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "simplicio").mkdir(parents=True)
    (repo / "simplicio" / "cli.py").write_text("print('ok')\n", encoding="utf-8")

    cmd = runner_mod._devcli_cmd(repo, "task", "--help")
    env = runner_mod._devcli_env(repo, {"PYTHONPATH": "BASE"})

    assert cmd[:3] == [sys.executable, "-m", "simplicio.cli"]
    assert cmd[3:] == ["task", "--help"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(repo)


def test_build_plan_uses_filtered_candidate_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "worker.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    task_payload = runner_mod.compile_many(TASK, source_path="task.md")
    tasks = task_payload["tasks"]
    mapper_payload = {
        "handoff": {
            "stdout": {
                "context_pack": {
                    "pack_hash": "pack-1",
                    "files": [
                        {"path": ".orchestrator/loop/runtime_run_task.md"},
                        {"path": "src/worker.py"},
                    ],
                }
            }
        },
        "repo_state_before": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "one"},
        "repo_state_after": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "two"},
        "generated_at": "2026-07-10T00:00:00Z",
    }

    plan = runner_mod._build_plan(tasks, mapper_payload, repo)
    assert plan["mapper_targets"] == ["src/worker.py"]
    assert plan["steps"][0]["candidate_targets"] == ["src/worker.py"]


def test_build_plan_promotes_explicit_task_file_hints(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "simplicio").mkdir()
    (repo / "tests" / "python").mkdir(parents=True)
    (repo / "simplicio" / "task_spec.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "python" / "test_task_spec.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    task_payload = runner_mod.compile_many(TASK, source_path="task.md")
    tasks = task_payload["tasks"]
    mapper_payload = {
        "handoff": {
            "stdout": {
                "context_pack": {
                    "pack_hash": "pack-1",
                    "files": [
                        {"path": "bench/compare_sp.py"},
                    ],
                }
            }
        },
        "repo_state_before": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "one"},
        "repo_state_after": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "two"},
        "generated_at": "2026-07-10T00:00:00Z",
    }

    plan = runner_mod._build_plan_with_hints(
        tasks,
        mapper_payload,
        repo,
        "Arquivos alvo: simplicio/task_spec.py tests/python/test_task_spec.py",
    )
    assert plan["steps"][0]["candidate_targets"][:2] == [
        "simplicio/task_spec.py",
        "tests/python/test_task_spec.py",
    ]


def test_arm_run_persists_state_for_status_resume_cancel(tmp_path, monkeypatch):
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path)
    payload = runner_mod.arm_run(str(repo), str(task), "verified", 9)
    run_id = payload["manifest"]["run_id"]
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "state.json").exists()
    assert (run_dir / "task-contract.json").exists()
    assert (run_dir / "mapper-preflight.json").exists()
    assert (run_dir / "operator-preflight.json").exists()
    assert (run_dir / "mapper-context.json").exists()
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "operator-receipt.json").exists()
    assert (run_dir / "delivery-receipt.json").exists()
    assert (run_dir / "loop" / "scratchpad.md").exists()
    assert (run_dir / "loop" / "watcher_challenge.json").exists()
    assert payload["state"]["phase"] == "awaiting_decision"
    assert payload["state"]["mapper"]["ready"] is True
    assert payload["state"]["operator"]["ready"] is True
    assert payload["state"]["operator"]["execution_state"] == "dry_run"
    assert payload["state"]["delivery"]["target"] == "verified"
    assert payload["state"]["delivery"]["current_state"] == "implemented"

    status = _run(CLI + ["status", "--repo", str(repo), "--run-id", run_id], REPO,
                  env={})
    assert status.returncode == 0, status.stdout + status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["state"]["coverage"]["scenarios"]["total"] == 1
    assert status_payload["state"]["coverage"]["rules"]["total"] == 1
    assert status_payload["state"]["mapper"]["receipt"].endswith("mapper-context.json")
    assert status_payload["state"]["operator"]["receipt"].endswith("operator-receipt.json")
    assert status_payload["state"]["delivery"]["receipt"].endswith("delivery-receipt.json")
    assert status_payload["state"]["completion"]["reason_code"] == "oracle_incomplete"

    resumed = _run(CLI + ["resume", "--repo", str(repo), run_id], REPO)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["state"]["phase"] == "awaiting_decision"
    assert len(resumed_payload["state"]["history"]) >= 2

    cancelled = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    cancelled_payload = json.loads(cancelled.stdout)
    assert cancelled_payload["state"]["phase"] == "cancelled"


def _start_run_for_maintenance_cli(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON", json.dumps({
        "version_stdout": "simplicio-mapper 0.19.0",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
        "version_returncode": 0,
        "help_returncode": 0,
    }))
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON", json.dumps({
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
        "help_returncode": 0,
    }))
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_OPERATOR_JSON", json.dumps({
        "execution_state": "dry_run", "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True}, "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"],
    }))
    armed = runner_mod.arm_run(str(repo), str(task), "verified", 12)
    return repo, armed["manifest"]["run_id"], Path(armed["run_dir"])


def test_maintenance_deferred_cli_serializes_mode_without_operator(tmp_path, monkeypatch):
    repo, run_id, run_dir = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    operator_receipt = run_dir / "operator-receipt.json"
    operator_before = operator_receipt.read_text(encoding="utf-8") if operator_receipt.exists() else None

    deferred = _run(CLI + [
        "maintenance-deferred", "--repo", str(repo), run_id,
        "--mode", "maintenance_deferred", "--disposition", "backlog_only",
        "--correction-summary", "Corrected the maintenance backlog entry.",
        "--deferral-reason", "Operator maintenance window is active.",
        "--resume-instruction", "Resume from the maintenance receipt.",
        "--resume-instruction", "Run the operator after maintenance.",
    ], REPO)
    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    status = json.loads(deferred.stdout)

    receipt = json.loads((run_dir / "maintenance-receipt.json").read_text(encoding="utf-8"))
    assert receipt["mode"] == "maintenance_deferred"
    assert receipt["disposition"] == "backlog_only"
    assert receipt["completion_ready"] is False
    if operator_before is None:
        assert not operator_receipt.exists()
    else:
        assert operator_receipt.read_text(encoding="utf-8") == operator_before
    assert status["state"]["completion"]["ready"] is False
    assert status["state"]["completion"]["tag"] == "UNVERIFIED"
    assert status["state"]["maintenance"]["mode"] == "maintenance_deferred"
    assert status["state"]["next_action"] == "resume_from_maintenance_receipt"


def test_maintenance_deferred_cli_rejects_non_deferred_mode(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "missing-run"
    rejected = _run(CLI + [
        "maintenance-deferred", "--repo", str(repo), run_id,
        "--mode", "active", "--disposition", "backlog_only",
        "--correction-summary", "summary", "--deferral-reason", "reason",
    ], REPO)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout) == {
        "ready": False,
        "reason_code": "maintenance_mode_invalid",
        "tag": "UNVERIFIED",
    }
    assert not (repo / ".orchestrator" / "runs" / run_id / "maintenance-receipt.json").exists()


def test_resume_rejects_terminal_cancelled_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    started = _arm_result(
        repo, task, env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator}
    )
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]
    cancelled = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr

    resumed = _run(CLI + ["resume", "--repo", str(repo), run_id], REPO)
    assert resumed.returncode != 0, resumed.stdout + resumed.stderr
    assert "run already terminal" in resumed.stderr or "run already terminal" in resumed.stdout


def test_cancel_rejects_terminal_cancelled_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    started = _arm_result(
        repo, task, env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator}
    )
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]
    cancelled = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr

    cancelled_again = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled_again.returncode != 0, cancelled_again.stdout + cancelled_again.stderr
    assert "run already terminal" in cancelled_again.stderr or "run already terminal" in cancelled_again.stdout


def test_status_surfaces_completion_receipt_reason_code(tmp_path, monkeypatch):
    repo, _, payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = payload["manifest"]["run_id"]
    loop_dir = run_dir / "loop"
    receipt_payload = {
        "ready": False,
        "verdict": "DELIVERY_PENDING",
        "reason_code": "watcher_mismatch",
        "reason": "watcher receipt is not MEASURED/matching",
        "tag": "UNVERIFIED",
        "gates": [],
    }
    persist_completion_receipt(receipt_payload, str(loop_dir), str(run_dir))

    status = _run(CLI + ["status", "--repo", str(repo), "--run-id", run_id], REPO)
    assert status.returncode == 0, status.stdout + status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["state"]["completion"]["receipt"].endswith("completion-receipt.json")
    assert status_payload["state"]["completion"]["reason_code"] == "watcher_mismatch"


def test_tick_executes_real_operator_boundary_and_binds_receipt(tmp_path, monkeypatch):
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 0, "stdout": {"kind": "operator-applied", "ok": True}, "stderr": "",
            "write_files": {"src/app.py": "def main():\n    return 'updated'\n"},
        }),
    }
    run_id = armed_payload["manifest"]["run_id"]
    with patch.dict(os.environ, env, clear=False):
        payload = runner_mod.execute_operator(str(repo), run_id)
    assert payload["state"]["phase"] == "validating"
    receipt = json.loads((run_dir / "operator-receipt.json").read_text(encoding="utf-8"))
    assert receipt["mode"] == "execute"
    assert receipt["execution_state"] == "applied"
    assert receipt["attempt"] == 1
    assert receipt["retry_budget"] == 3
    assert receipt["failure_fingerprint"] == ""
    assert receipt["task_contract_hash"]
    assert receipt["plan_hash"]
    assert receipt["target_within_repo"] is True


def test_tick_rolls_back_failed_operator_when_change_stays_within_authorized_target(tmp_path, monkeypatch):
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    target = repo / "src" / "app.py"
    original = target.read_text(encoding="utf-8")
    env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 1,
            "stdout": {"kind": "operator-applied", "ok": False},
            "stderr": "validation failed",
            "write_files": {"src/app.py": "def main():\n    return 'broken'\n"},
        }),
    }
    run_id = armed_payload["manifest"]["run_id"]
    with patch.dict(os.environ, env, clear=False):
        payload = runner_mod.execute_operator(str(repo), run_id)
    assert payload["state"]["phase"] == "blocked"
    assert target.read_text(encoding="utf-8") == original
    receipt = json.loads((run_dir / "operator-receipt.json").read_text(encoding="utf-8"))
    assert receipt["attempt"] == 1
    assert receipt["retry_budget"] == 3
    assert receipt["failure_fingerprint"]
    assert receipt["rollback"]["attempted"] is True
    assert receipt["rollback"]["restored"] is True
    assert receipt["changed_paths"] == []
    assert receipt["checkpoint"]["safe_targets"] == ["src/app.py"]


def test_deliver_reconciles_external_delivery_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    fake_mapper_preflight = json.dumps({
        "version_stdout": "simplicio-mapper 0.19.0",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
        "version_returncode": 0,
        "help_returncode": 0
    })
    fake_devcli_preflight = json.dumps({
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
        "help_returncode": 0
    })
    started = _arm_result(
        repo, task, delivery="merge-ready", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
             "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
             "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    source_payload = {
        "pr": {"url": "https://example/pr/99", "head_sha": "abc123", "base_sha": "def456"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True}
    }
    evidence_file = tmp_path / "merge-ready.json"
    evidence_file.write_text(json.dumps(source_payload), encoding="utf-8")
    delivered = _run(CLI + ["deliver", "--repo", str(repo), run_id, "--state", "merge-ready",
                            "--source-kind", "github", "--payload-file", str(evidence_file)], REPO)
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    delivered_payload = json.loads(delivered.stdout)
    assert delivered_payload["state"]["delivery"]["current_state"] == "merge-ready"
    assert delivered_payload["state"]["delivery"]["ready"] is True
    assert delivered_payload["state"]["phase"] == "delivering"


def test_decide_invalidates_plan_and_receipts_after_human_answer(tmp_path, monkeypatch):
    task_text = TASK + """

3. Requisitos Não Funcionais

Nenhum requisito não-funcional identificado na entrada — validar com o time.

6. Dependências

Nenhuma dependência identificada na entrada — validar com o time.

7. Sinais de Impacto

Frontend: ✓
Backend: Possível
Banco: ✗
Integrações: ✗
"""
    repo, _, payload, run_dir = _arm_deterministic_preflight_fixture(
        monkeypatch, tmp_path, task_text=task_text,
    )
    run_id = payload["manifest"]["run_id"]
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "operator-receipt.json").exists()
    decided = _run(CLI + ["decide", "--repo", str(repo), run_id, "--decision-id", "Q-LAYER-1",
                          "--answer", "Implementar no frontend apenas", "--impact", "behavior-change"], REPO)
    assert decided.returncode == 0, decided.stdout + decided.stderr
    decided_payload = json.loads(decided.stdout)
    assert decided_payload["state"]["phase"] == "awaiting_decision"
    assert decided_payload["state"]["operator"]["execution_state"] == "invalidated"
    assert decided_payload["state"]["evidence"]["status"] == "INVALIDATED"
    assert not (run_dir / "plan.json").exists()
    assert not (run_dir / "operator-receipt.json").exists()
    assert not (run_dir / "evidence-receipt.json").exists()
    contract = json.loads((run_dir / "task-contract.json").read_text(encoding="utf-8"))
    task0 = contract["tasks"][0]
    match = next(item for item in task0["decision_ledger"] if item["id"] == "Q-LAYER-1")
    assert match["resolved"] is True
    assert match["answer"] == "Implementar no frontend apenas"


def test_sync_source_requeries_github_fixture_for_merge_ready(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    fake_mapper_preflight = json.dumps({
        "version_stdout": "simplicio-mapper 0.19.0",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
        "version_returncode": 0,
        "help_returncode": 0
    })
    fake_devcli_preflight = json.dumps({
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
        "help_returncode": 0
    })
    started = _arm_result(
        repo, task, delivery="merge-ready", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
             "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
             "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    fixture = json.dumps({
        "pr": {"url": "https://example/pr/77", "head_sha": "abc123", "base_sha": "def456", "evidence": "github-pr-view"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True}
    })
    synced = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                         "--external-repo", "wesleysimplicio/simplicio-loop", "--pr", "77"], REPO,
                  env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": fixture})
    assert synced.returncode == 0, synced.stdout + synced.stderr
    synced_payload = json.loads(synced.stdout)
    assert synced_payload["state"]["delivery"]["current_state"] == "merge-ready"
    assert synced_payload["state"]["delivery"]["ready"] is True
    assert synced_payload["state"]["phase"] == "delivering"
    receipt = json.loads((Path(payload["run_dir"]) / "delivery-receipt.json").read_text(encoding="utf-8"))
    assert receipt["source_payload"]["source_query"]["provider"] == "github"
    assert receipt["source_payload"]["source_query"]["repo"] == "wesleysimplicio/simplicio-loop"


def test_sync_source_requeries_github_fixture_for_release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    fake_mapper_preflight = json.dumps({
        "version_stdout": "simplicio-mapper 0.19.0",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
        "version_returncode": 0,
        "help_returncode": 0
    })
    fake_devcli_preflight = json.dumps({
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
        "help_returncode": 0
    })
    started = _arm_result(
        repo, task, delivery="released", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
             "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
             "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    fixture = json.dumps({
        "release": {
            "tag": "v1.2.3",
            "assets": ["simplicio-loop.whl"],
            "checksums_verified": True,
            "signatures_verified": True,
            "sbom_present": True
        },
        "install_smoke": {"passed": True}
    })
    synced = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                         "--external-repo", "wesleysimplicio/simplicio-loop", "--tag", "v1.2.3"], REPO,
                  env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": fixture})
    assert synced.returncode == 0, synced.stdout + synced.stderr
    synced_payload = json.loads(synced.stdout)
    assert synced_payload["state"]["delivery"]["current_state"] == "released"
    assert synced_payload["state"]["delivery"]["ready"] is True


def test_sync_source_reopens_delivery_when_merge_ready_regresses(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    fake_mapper_preflight = json.dumps({
        "version_stdout": "simplicio-mapper 0.19.0",
        "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
        "version_returncode": 0,
        "help_returncode": 0
    })
    fake_devcli_preflight = json.dumps({
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
        "help_returncode": 0
    })
    started = _arm_result(
        repo, task, delivery="merge-ready", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
             "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
             "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]

    ready_fixture = json.dumps({
        "pr": {"url": "https://example/pr/77", "head_sha": "abc123", "base_sha": "def456", "evidence": "github-pr-view"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True}
    })
    ready = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                        "--external-repo", "wesleysimplicio/simplicio-loop", "--pr", "77"], REPO,
                 env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": ready_fixture})
    assert ready.returncode == 0, ready.stdout + ready.stderr
    ready_payload = json.loads(ready.stdout)
    assert ready_payload["state"]["delivery"]["ready"] is True
    assert ready_payload["state"]["phase"] == "delivering"

    regressed_fixture = json.dumps({
        "pr": {"url": "https://example/pr/77", "head_sha": "abc123", "base_sha": "def456", "evidence": "github-pr-view"},
        "checks": {"green": False},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True}
    })
    regressed = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                            "--external-repo", "wesleysimplicio/simplicio-loop", "--pr", "77"], REPO,
                     env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": regressed_fixture})
    assert regressed.returncode == 0, regressed.stdout + regressed.stderr
    regressed_payload = json.loads(regressed.stdout)
    assert regressed_payload["state"]["delivery"]["ready"] is False
    assert regressed_payload["state"]["phase"] == "partial"
    assert "not green" in regressed_payload["state"]["blockers"][0].lower()


def test_run_blocks_when_mapper_preflight_version_too_old(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    started = _run(
        CLI + ["run", "--repo", str(repo), "--task", str(task), "--delivery", "verified", "--max-iterations", "9"],
        REPO,
        env={
            "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": json.dumps({
                "version_stdout": "simplicio-mapper 0.13.9",
                "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
                "version_returncode": 0,
                "help_returncode": 0
            }),
            "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": json.dumps({
                "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target",
                "help_returncode": 0
            })
        },
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    assert payload["state"]["phase"] == "blocked"
    assert "minimum version" in payload["state"]["blockers"][0]


@pytest.mark.parametrize("missing_capability", ["--bound-paths", "--target"])
def test_run_blocks_when_devcli_preflight_lacks_required_capability(
    tmp_path, monkeypatch, missing_capability,
):
    surface = (
        "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target"
    ).replace(missing_capability, "")
    _, task = _setup_deterministic_preflight_fixture(
        monkeypatch,
        tmp_path,
        operator_preflight_surface=surface,
    )
    payload = runner_mod.arm_run(str(tmp_path / "repo"), str(task), "verified", 9)

    assert payload["state"]["phase"] == "blocked"
    assert "required capabilities" in payload["state"]["blockers"][0]
    receipt = json.loads(
        (Path(payload["run_dir"]) / "operator-preflight.json").read_text(encoding="utf-8")
    )
    assert receipt["missing_tokens"] == []
    assert receipt["missing_capabilities"] == [missing_capability]


def test_sync_source_infers_pr_open_when_merge_ready_target_is_not_yet_satisfied(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    started = _arm_result(
        repo, task, delivery="merge-ready", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    fixture = json.dumps({
        "pr": {"url": "https://example/pr/77", "head_sha": "abc123", "base_sha": "def456", "evidence": "github-pr-view"},
        "checks": {"green": False},
        "reviews": {"approvals": 0, "open_threads": 2},
        "branch": {"up_to_date": False}
    })
    synced = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                         "--external-repo", "wesleysimplicio/simplicio-loop", "--pr", "77"], REPO,
                  env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": fixture})
    assert synced.returncode == 0, synced.stdout + synced.stderr
    synced_payload = json.loads(synced.stdout)
    assert synced_payload["state"]["delivery"]["current_state"] == "pr-open"
    assert synced_payload["state"]["delivery"]["ready"] is False
    assert synced_payload["state"]["phase"] == "partial"
    assert "merge-ready" in synced_payload["state"]["blockers"][0]


def test_sync_source_infers_merged_even_when_target_is_merge_ready(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    fake_operator = json.dumps({
        "execution_state": "dry_run",
        "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True},
        "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"]
    })
    started = _arm_result(
        repo, task, delivery="merge-ready", max_iterations=9,
        env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator},
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    fixture = json.dumps({
        "pr": {"url": "https://example/pr/77", "head_sha": "abc123", "base_sha": "def456", "evidence": "github-pr-view"},
        "checks": {"green": True},
        "reviews": {"approvals": 1, "open_threads": 0},
        "branch": {"up_to_date": True},
        "merge": {
            "commit_sha": "deadbeef",
            "default_branch": "main",
            "merged_at": "2026-07-10T01:00:00Z",
            "commit_in_default_branch": True
        }
    })
    synced = _run(CLI + ["sync-source", "--repo", str(repo), run_id, "--source", "github",
                         "--external-repo", "wesleysimplicio/simplicio-loop", "--pr", "77"], REPO,
                  env={"SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON": fixture})
    assert synced.returncode == 0, synced.stdout + synced.stderr
    synced_payload = json.loads(synced.stdout)
    assert synced_payload["state"]["delivery"]["current_state"] == "merged"
    assert synced_payload["state"]["delivery"]["ready"] is True


def test_maintenance_deferred_invalidates_ready_completion_receipt(tmp_path, monkeypatch):
    repo, run_id, run_dir = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    (run_dir / "completion-receipt.json").write_text(json.dumps({"ready": True, "verdict": "COMPLETE", "tag": "MEASURED"}), encoding="utf-8")

    result = runner_mod.defer_maintenance_backlog_only(
        str(repo), run_id, correction_summary="stale receipt",
        deferral_reason="maintenance window", resume_instructions=["resume later"],
    )

    persisted = json.loads((run_dir / "completion-receipt.json").read_text(encoding="utf-8"))
    assert persisted["ready"] is False
    assert persisted["verdict"] == "DELIVERY_PENDING"
    assert persisted["reason_code"] == "maintenance_deferred"
    assert result["state"]["completion"]["ready"] is False


def test_maintenance_deferred_rejects_terminal_run(tmp_path, monkeypatch):
    repo, run_id, _ = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    runner_mod.change_phase(str(repo), run_id, "done", "test terminal")

    with pytest.raises(ValueError, match="run already terminal"):
        runner_mod.defer_maintenance_backlog_only(
            str(repo), run_id, correction_summary="late",
            deferral_reason="maintenance", resume_instructions=["resume"],
        )


def test_maintenance_deferred_blocks_operator_and_batch(tmp_path, monkeypatch):
    repo, run_id, _ = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    runner_mod.defer_maintenance_backlog_only(
        str(repo), run_id, correction_summary="freeze",
        deferral_reason="maintenance", resume_instructions=["resume"],
    )

    with pytest.raises(RuntimeError, match="maintenance deferred"):
        runner_mod.execute_operator(str(repo), run_id)
    with pytest.raises(RuntimeError, match="maintenance deferred"):
        runner_mod.execute_operator_batch(str(repo), run_id)


def test_resume_clears_maintenance_backlog_only_transition(tmp_path, monkeypatch):
    repo, run_id, _ = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    runner_mod.defer_maintenance_backlog_only(
        str(repo), run_id, correction_summary="freeze",
        deferral_reason="maintenance", resume_instructions=["resume"],
    )

    resumed = _run(CLI + ["resume", "--repo", str(repo), run_id], REPO)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = json.loads(resumed.stdout)

    assert payload["state"]["phase"] == "awaiting_decision"
    assert payload["state"]["maintenance"]["mode"] == "active"
    assert payload["state"]["maintenance"]["disposition"] == "operator"
    assert payload["state"]["operator"]["ready"] is False
    assert payload["state"]["operator"]["execution_state"] == "invalidated"
    assert payload["state"]["evidence"]["ready"] is False
    assert payload["state"]["evidence"]["status"] == "INVALIDATED"
    assert payload["state"]["next_action"] == "mapper_scan_required"


def _setup_deterministic_preflight_fixture(
    monkeypatch, tmp_path, *, targets=True, operator=None, task_text=TASK,
    operator_preflight_surface=None,
):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".simplicio/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore", "src/app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Simplicio Tests", "-c", "user.email=tests@simplicio.local",
         "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    task = tmp_path / "task.md"
    task.write_text(task_text, encoding="utf-8")
    fingerprint = {
        "head": "head-fixed",
        "tree_hash": "tree-fixed",
        "dirty_status_hash": "status-fixed",
    }
    monkeypatch.setattr(runner_mod, "_now", lambda: "2026-07-14T00:00:00Z")
    monkeypatch.setattr(runner_mod, "_run_id", lambda: "run-fixed")
    monkeypatch.setattr(runner_mod, "_rand_token", lambda size: "token-fixed")
    monkeypatch.setattr(runner_mod, "_repo_fingerprint", lambda path: dict(fingerprint))
    monkeypatch.setattr(
        runner_mod,
        "_changed_paths",
        lambda path: (
            ["src/app.py"]
            if (Path(path) / "src" / "app.py").read_text(encoding="utf-8")
            != "def main():\n    return 'ok'\n"
            else []
        ),
    )

    def fake_mapper(repo_path, run_root, **kwargs):
        runner_mod._write_json(run_root / "mapper-preflight.json", {
            "tool": "simplicio-mapper", "identity_ok": True, "version_ok": True,
            "missing_verbs": [], "repo_state": dict(fingerprint),
        })
        files = [{"path": "src/app.py", "tests": []}] if targets else []
        payload = {
            "scan": {"returncode": 0, "stdout": {}, "stderr": ""},
            "inspect": {"returncode": 0, "stdout": {
                "status": {"artifacts_present": True, "fresh": True},
                "evidence": {"artifacts": {
                    "project_map": {"exists": True},
                    "precedent_index": {"exists": True},
                }},
            }, "stderr": ""},
            "handoff": {"returncode": 0, "stdout": {
                "context_pack": {"pack_hash": "pack-fixed", "files": files},
            }, "stderr": ""},
            "generated_at": "2026-07-14T00:00:00Z",
            "repo_state_before": dict(fingerprint),
            "repo_state_after": dict(fingerprint),
        }
        runner_mod._write_json(run_root / "mapper-context.json", payload)
        return payload

    def fake_operator_preflight(repo_path, run_root):
        help_surface = "Usage: simplicio-dev-cli task --dry-run-task --json --bound-paths --target"
        receipt = {
            "tool": "simplicio-dev-cli", "identity_ok": True, "version_ok": True,
            "help_stdout": help_surface, "task_help_stdout": help_surface,
            "required_tokens": list(runner_mod.DEVCLI_REQUIRED_TOKENS),
            "missing_tokens": [],
            "required_capabilities": list(runner_mod.DEVCLI_REQUIRED_CAPABILITIES),
            "missing_capabilities": [],
            "repo_state": dict(fingerprint),
        }
        runner_mod._write_json(run_root / "operator-preflight.json", receipt)
        return receipt

    monkeypatch.setattr(runner_mod, "_run_mapper", fake_mapper)
    if operator_preflight_surface is None:
        monkeypatch.setattr(runner_mod, "_preflight_operator", fake_operator_preflight)
    else:
        monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON", json.dumps({
            "help_stdout": operator_preflight_surface,
            "help_returncode": 0,
            "version_stdout": "simplicio-py 0.14.0",
            "version_returncode": 0,
        }))
    if not targets:
        monkeypatch.setattr(runner_mod, "_fallback_targets", lambda path: [])
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_OPERATOR_JSON", json.dumps(operator or {
        "execution_state": "dry_run", "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True}, "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"],
    }))
    return repo, task


def _arm_deterministic_preflight_fixture(monkeypatch, tmp_path, **kwargs):
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path, **kwargs)
    armed = runner_mod.arm_run(str(repo), str(task), "verified", 12)
    return repo, task, armed, Path(armed["run_dir"])


def test_arm_run_blocks_when_plan_has_no_authorized_target(tmp_path, monkeypatch):
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path, targets=False)
    monkeypatch.setattr(runner_mod, "validate_plan", lambda *args, **kwargs: {
        "valid": True, "errors": [], "warnings": [],
    })
    armed = runner_mod.arm_run(str(repo), str(task), "verified", 12)
    run_dir = Path(armed["run_dir"])

    assert armed["state"]["phase"] == "blocked"
    assert "no authorized operator target" in armed["state"]["history"][-1]["extra"]["error"]
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "plan.json").exists()
    assert not (run_dir / "operator-receipt.json").exists()


def test_arm_run_blocks_when_operator_dry_run_fails(tmp_path, monkeypatch):
    _, _, armed, run_dir = _arm_deterministic_preflight_fixture(
        monkeypatch, tmp_path,
        operator={"execution_state": "blocked", "returncode": 1,
                   "stdout": {}, "stderr": "dry run failed"},
    )

    assert armed["state"]["phase"] == "blocked"
    assert "dry run failed" in armed["state"]["history"][-1]["extra"]["error"]
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "operator-receipt.json").exists()


def test_execute_operator_batch_blocks_before_dispatch_when_receipt_missing_or_stale(tmp_path, monkeypatch):
    for case in ("missing", "stale"):
        case_dir = tmp_path / case
        repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, case_dir)
        operator_receipt = run_dir / "operator-receipt.json"
        if case == "missing":
            operator_receipt.unlink()
        else:
            payload = json.loads(operator_receipt.read_text(encoding="utf-8"))
            payload["plan_hash"] = "stale-plan"
            operator_receipt.write_text(json.dumps(payload), encoding="utf-8")
        dispatched = []
        monkeypatch.setattr(
            runner_mod, "dispatch_operator_batch",
            lambda *args, **kwargs: dispatched.append((args, kwargs)),
        )

        with pytest.raises(RuntimeError):
            runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

        diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
        assert diagnostic["schema"] == "simplicio.operator-batch-preflight/v1"
        assert diagnostic["status"] == "BLOCKED"
        assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["phase"] == "blocked"
        assert dispatched == []
        assert not (run_dir / "operator-batch.jsonl").exists()
        assert not list(run_dir.glob("*dead*letter*"))


def test_conduct_run_blocked_preflight_creates_no_batch_dead_letters(tmp_path, monkeypatch):
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path, targets=False)

    result = runner_mod.conduct_run(str(repo), str(task))
    run_dir = Path(result["run_dir"])

    assert result["state"]["phase"] == "blocked"
    assert not (run_dir / "operator-batch.jsonl").exists()
    assert not list(run_dir.glob("*dead*letter*"))


def test_conduct_run_fails_explicitly_blocked_when_batch_preflight_raises(tmp_path, monkeypatch):
    """Issue #279 regression: a fully armed run whose batch boundary rejects a stale or
    unbound receipt chain must surface as an explicit ``blocked`` result from `run` -- never
    as an uncaught exception that leaves the run in an undiagnosed, partially armed state.
    """
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path)

    def fail_batch(repo_arg, run_id, **kwargs):
        run_dir = Path(repo_arg) / ".simplicio" / "loop-runs" / run_id
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        runner_mod._persist_batch_preflight_block(
            run_dir, state, Path(repo_arg), "stale operator receipt: repository changed",
        )
        raise RuntimeError("stale operator receipt: repository changed")

    monkeypatch.setattr(runner_mod, "execute_operator_batch", fail_batch)

    result = runner_mod.conduct_run(str(repo), str(task))
    run_dir = Path(result["run_dir"])

    assert result["state"]["phase"] == "blocked"
    assert "stale operator receipt" in json.dumps(result["state"]["history"][-1])
    assert (run_dir / "operator-batch-preflight.json").exists()
    assert not (run_dir / "operator-batch.jsonl").exists()
    assert not list(run_dir.glob("*dead*letter*"))


def test_conduct_run_force_blocks_when_batch_boundary_raises_without_diagnostic(tmp_path, monkeypatch):
    """Defensive fallback: even if the batch boundary somehow raises without persisting its
    own blocked diagnostic, `run` must still surface an explicit blocked result rather than
    propagating the exception or leaving the run's phase ambiguous.
    """
    repo, task = _setup_deterministic_preflight_fixture(monkeypatch, tmp_path)

    def fail_batch_no_diagnostic(repo_arg, run_id, **kwargs):
        raise RuntimeError("unexpected batch boundary failure")

    monkeypatch.setattr(runner_mod, "execute_operator_batch", fail_batch_no_diagnostic)

    result = runner_mod.conduct_run(str(repo), str(task))

    assert result["state"]["phase"] == "blocked"
    assert "unexpected batch boundary failure" in json.dumps(result["state"]["history"][-1])


def test_execute_operator_batch_accepts_fresh_run_receipt_chain(tmp_path, monkeypatch):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    dispatched = []

    def fake_dispatch(items, **kwargs):
        dispatched.extend(list(items))
        return {"failed_task_indices": [], "dead_letter_task_indices": []}

    monkeypatch.setattr(runner_mod, "dispatch_operator_batch", fake_dispatch)
    result = runner_mod.execute_operator_batch(
        str(repo), armed["manifest"]["run_id"], max_workers=1,
        isolated_contexts={1: {"isolation": "shared"}}, auto_fan_out=False,
    )

    assert result["failed_task_indices"] == []
    assert len(dispatched) == 1
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "mapper-context.json").exists()
    assert (run_dir / "operator-preflight.json").exists()
    assert (run_dir / "operator-receipt.json").exists()
    operator_preflight = json.loads(
        (run_dir / "operator-preflight.json").read_text(encoding="utf-8")
    )
    assert operator_preflight["required_tokens"] == list(runner_mod.DEVCLI_REQUIRED_TOKENS)
    assert operator_preflight["required_capabilities"] == list(runner_mod.DEVCLI_REQUIRED_CAPABILITIES)
    assert operator_preflight["missing_tokens"] == []
    assert operator_preflight["missing_capabilities"] == []


@pytest.mark.parametrize(
    ("receipt_name", "field", "value", "message"),
    [
        ("mapper-context.json", "run_id", "run-other", "not bound to the current run"),
        ("plan.json", "run_id", "run-other", "not bound to the current run"),
        ("plan.json", "mapper_context_hash", "", "no mapper context hash"),
        ("operator-receipt.json", "run_id", "run-other", "not bound to the current run"),
        ("operator-receipt.json", "mapper_context_hash", "tampered", "mapper receipt"),
        ("operator-receipt.json", "returncode", 1, "successful dry-run"),
        ("operator-receipt.json", "target", "../outside.py", "authorized repository"),
    ],
)
def test_batch_rejects_cross_run_and_tampered_receipt_bindings(
    tmp_path, monkeypatch, receipt_name, field, value, message,
):
    case_dir = tmp_path / (receipt_name.replace(".json", "") + "-" + field)
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, case_dir)
    receipt_path = run_dir / receipt_name
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field] = value
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"
    assert diagnostic["blocker"]["reason_code"] == "operator_batch_preflight_failed"
    assert not (run_dir / "operator-batch.jsonl").exists()
    assert not list(run_dir.glob("*dead*letter*"))


def test_batch_rejects_mapper_context_byte_tamper(tmp_path, monkeypatch):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    mapper_path = run_dir / "mapper-context.json"
    mapper = json.loads(mapper_path.read_text(encoding="utf-8"))
    mapper["generated_at"] = "2026-07-14T00:00:01Z"
    mapper_path.write_text(json.dumps(mapper), encoding="utf-8")

    with pytest.raises(RuntimeError, match="current mapper context bytes"):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"
    assert not (run_dir / "operator-batch.jsonl").exists()


@pytest.mark.parametrize("missing_capability", ["--bound-paths", "--target"])
def test_batch_rejects_operator_preflight_missing_capability(
    tmp_path, monkeypatch, missing_capability,
):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    preflight_path = run_dir / "operator-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["help_stdout"] = preflight["help_stdout"].replace(missing_capability, "")
    preflight["task_help_stdout"] = preflight["task_help_stdout"].replace(missing_capability, "")
    preflight["missing_capabilities"] = [missing_capability]
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing required capabilities"):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"
    assert not (run_dir / "operator-batch.jsonl").exists()


@pytest.mark.parametrize(
    "missing_field",
    [
        "required_tokens", "missing_tokens", "required_capabilities", "missing_capabilities",
        "help_stdout", "task_help_stdout", "__all__",
    ],
)
def test_batch_rejects_operator_preflight_missing_contract_field(
    tmp_path, monkeypatch, missing_field,
):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    preflight_path = run_dir / "operator-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    fields = (
        "required_tokens", "missing_tokens", "required_capabilities", "missing_capabilities",
        "help_stdout", "task_help_stdout",
    )
    for field in fields if missing_field == "__all__" else (missing_field,):
        preflight.pop(field)
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing required field"):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"
    assert not (run_dir / "operator-batch.jsonl").exists()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("required_tokens", "not-a-list"),
        ("missing_tokens", None),
        ("required_capabilities", {}),
        ("missing_capabilities", [1]),
        ("help_stdout", []),
        ("task_help_stdout", None),
    ],
)
def test_batch_rejects_operator_preflight_invalid_contract_field_type(
    tmp_path, monkeypatch, field, invalid_value,
):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    preflight_path = run_dir / "operator-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight[field] = invalid_value
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid field type"):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"


@pytest.mark.parametrize(
    ("missing_surface", "message"),
    [
        ("--json", "missing_tokens do not match persisted help"),
        ("--target", "missing_capabilities do not match persisted help"),
    ],
)
def test_batch_rejects_forged_empty_capability_gaps_against_deficient_help(
    tmp_path, monkeypatch, missing_surface, message,
):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    preflight_path = run_dir / "operator-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["help_stdout"] = preflight["help_stdout"].replace(missing_surface, "")
    preflight["task_help_stdout"] = preflight["task_help_stdout"].replace(missing_surface, "")
    preflight["missing_tokens"] = []
    preflight["missing_capabilities"] = []
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"


@pytest.mark.parametrize("field", ["required_tokens", "required_capabilities"])
def test_batch_rejects_altered_operator_preflight_required_contract(
    tmp_path, monkeypatch, field,
):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    preflight_path = run_dir / "operator-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight[field] = [*preflight[field], "--forged-capability"]
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(RuntimeError, match="canonical contract"):
        runner_mod.execute_operator_batch(str(repo), armed["manifest"]["run_id"])

    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "BLOCKED"
    assert diagnostic["blocker"]["scope"] == "global"


def test_direct_dispatch_cannot_bypass_run_global_preflight(tmp_path, monkeypatch):
    repo, _, armed, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    operator_path = run_dir / "operator-receipt.json"
    operator = json.loads(operator_path.read_text(encoding="utf-8"))
    operator["plan_hash"] = "tampered"
    operator_path.write_text(json.dumps(operator), encoding="utf-8")
    prepared = []
    monkeypatch.setattr(
        runner_mod, "_prepare_worktree_contexts",
        lambda *args, **kwargs: prepared.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="current plan"):
        runner_mod.dispatch_operator_batch(
            [{"repo": str(repo), "run_id": armed["manifest"]["run_id"], "task_index": 1}],
            journal_dir=str(run_dir),
        )

    assert prepared == []
    diagnostic = json.loads((run_dir / "operator-batch-preflight.json").read_text(encoding="utf-8"))
    assert diagnostic["blocker"]["scope"] == "global"
    assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["phase"] == "blocked"
    assert not (run_dir / "operator-batch.jsonl").exists()
    assert not list(run_dir.glob("*dead*letter*"))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_runner_cli")
