import json
import os
import subprocess
import sys
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


def test_run_arms_persisted_state_and_status_resume_cancel(tmp_path):
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "verified", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    run_dir = repo / ".orchestrator" / "runs" / run_id
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
                  env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                       "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                       "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0,
    }))
    monkeypatch.setenv("SIMPLICIO_LOOP_FAKE_OPERATOR_JSON", json.dumps({
        "execution_state": "dry_run", "returncode": 0,
        "stdout": {"kind": "operator-proposal", "ok": True}, "stderr": "",
        "argv": ["simplicio-dev-cli", "task", "demo"],
    }))

    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task)], REPO)
    assert started.returncode == 0, started.stdout + started.stderr
    return repo, json.loads(started.stdout)["manifest"]["run_id"]


def test_maintenance_deferred_cli_serializes_mode_without_operator(tmp_path, monkeypatch):
    repo, run_id = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    run_dir = repo / ".orchestrator" / "runs" / run_id
    operator_receipt = run_dir / "operator-receipt.json"
    operator_before = operator_receipt.read_text(encoding="utf-8")

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
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task)], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator})
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
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task)], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator})
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]
    cancelled = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr

    cancelled_again = _run(CLI + ["cancel", "--repo", str(repo), run_id], REPO)
    assert cancelled_again.returncode != 0, cancelled_again.stdout + cancelled_again.stderr
    assert "run already terminal" in cancelled_again.stderr or "run already terminal" in cancelled_again.stdout


def test_status_surfaces_completion_receipt_reason_code(tmp_path):
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "verified", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    run_dir = repo / ".orchestrator" / "runs" / run_id
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


def test_tick_executes_real_operator_boundary_and_binds_receipt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": json.dumps({
            "execution_state": "dry_run", "returncode": 0,
            "stdout": {"kind": "operator-proposal", "ok": True}, "stderr": "",
        }),
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 0, "stdout": {"kind": "operator-applied", "ok": True}, "stderr": "",
        }),
        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": json.dumps({
            "version_stdout": "simplicio-mapper 0.19.0",
            "help_stdout": "inspect handoff ask sync drift", "version_returncode": 0,
            "help_returncode": 0,
        }),
        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": json.dumps({
            "help_stdout": "task --dry-run-task --json", "help_returncode": 0,
        }),
    }
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task)], REPO, env=env)
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]
    ticked = _run(CLI + ["tick", "--repo", str(repo), run_id], REPO, env=env)
    assert ticked.returncode == 0, ticked.stdout + ticked.stderr
    payload = json.loads(ticked.stdout)
    assert payload["state"]["phase"] == "validating"
    receipt = json.loads((repo / ".orchestrator" / "runs" / run_id / "operator-receipt.json").read_text(encoding="utf-8"))
    assert receipt["mode"] == "execute"
    assert receipt["execution_state"] == "applied"
    assert receipt["attempt"] == 1
    assert receipt["retry_budget"] == 3
    assert receipt["failure_fingerprint"] == ""
    assert receipt["task_contract_hash"]
    assert receipt["plan_hash"]
    assert receipt["target_within_repo"] is True


def test_tick_rolls_back_failed_operator_when_change_stays_within_authorized_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    target = src / "app.py"
    target.write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(TASK, encoding="utf-8")
    env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": json.dumps({
            "execution_state": "dry_run", "returncode": 0,
            "stdout": {"kind": "operator-proposal", "ok": True}, "stderr": "",
        }),
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 1,
            "stdout": {"kind": "operator-applied", "ok": False},
            "stderr": "validation failed",
            "write_files": {"src/app.py": "def main():\n    return 'broken'\n"},
        }),
        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": json.dumps({
            "version_stdout": "simplicio-mapper 0.19.0",
            "help_stdout": "inspect handoff ask sync drift", "version_returncode": 0,
            "help_returncode": 0,
        }),
        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": json.dumps({
            "help_stdout": "task --dry-run-task --json", "help_returncode": 0,
        }),
    }
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task)], REPO, env=env)
    assert started.returncode == 0, started.stdout + started.stderr
    run_id = json.loads(started.stdout)["manifest"]["run_id"]
    ticked = _run(CLI + ["tick", "--repo", str(repo), run_id], REPO, env=env)
    assert ticked.returncode == 0, ticked.stdout + ticked.stderr
    payload = json.loads(ticked.stdout)
    assert payload["state"]["phase"] == "blocked"
    assert target.read_text(encoding="utf-8") == original
    receipt = json.loads((repo / ".orchestrator" / "runs" / run_id / "operator-receipt.json").read_text(encoding="utf-8"))
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "merge-ready", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
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


def test_decide_invalidates_plan_and_receipts_after_human_answer(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(
        TASK + """

3. Requisitos Não Funcionais

Nenhum requisito não-funcional identificado na entrada — validar com o time.

6. Dependências

Nenhuma dependência identificada na entrada — validar com o time.

7. Sinais de Impacto

Frontend: ✓
Backend: Possível
Banco: ✗
Integrações: ✗
""",
        encoding="utf-8",
    )
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "verified", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    run_id = payload["manifest"]["run_id"]
    run_dir = repo / ".orchestrator" / "runs" / run_id
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "merge-ready", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
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
    receipt = json.loads((repo / ".orchestrator" / "runs" / run_id / "delivery-receipt.json").read_text(encoding="utf-8"))
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "released", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
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
        "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
        "help_returncode": 0
    })
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "merge-ready", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator,
                        "SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON": fake_mapper_preflight,
                        "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": fake_devcli_preflight})
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
                "help_stdout": "Usage: simplicio-dev-cli task --dry-run-task --json",
                "help_returncode": 0
            })
        },
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    assert payload["state"]["phase"] == "blocked"
    assert "minimum version" in payload["state"]["blockers"][0]


def test_run_blocks_when_devcli_preflight_lacks_required_capability(tmp_path):
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
                "version_stdout": "simplicio-mapper 0.19.0",
                "help_stdout": "Usage: simplicio-mapper inspect handoff ask sync drift",
                "version_returncode": 0,
                "help_returncode": 0
            }),
            "SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON": json.dumps({
                "help_stdout": "Usage: simplicio-dev-cli",
                "help_returncode": 0
            })
        },
    )
    assert started.returncode == 0, started.stdout + started.stderr
    payload = json.loads(started.stdout)
    assert payload["state"]["phase"] == "blocked"
    assert "required capabilities" in payload["state"]["blockers"][0]


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
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "merge-ready", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator})
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
    started = _run(CLI + ["run", "--repo", str(repo), "--task", str(task),
                          "--delivery", "merge-ready", "--max-iterations", "9"], REPO,
                   env={"SIMPLICIO_LOOP_FAKE_OPERATOR_JSON": fake_operator})
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
    repo, run_id = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    run_dir = repo / ".orchestrator" / "runs" / run_id
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
    repo, run_id = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    runner_mod.change_phase(str(repo), run_id, "done", "test terminal")

    with pytest.raises(ValueError, match="run already terminal"):
        runner_mod.defer_maintenance_backlog_only(
            str(repo), run_id, correction_summary="late",
            deferral_reason="maintenance", resume_instructions=["resume"],
        )


def test_maintenance_deferred_blocks_operator_and_batch(tmp_path, monkeypatch):
    repo, run_id = _start_run_for_maintenance_cli(tmp_path, monkeypatch)
    runner_mod.defer_maintenance_backlog_only(
        str(repo), run_id, correction_summary="freeze",
        deferral_reason="maintenance", resume_instructions=["resume"],
    )

    with pytest.raises(RuntimeError, match="maintenance deferred"):
        runner_mod.execute_operator(str(repo), run_id)
    with pytest.raises(RuntimeError, match="maintenance deferred"):
        runner_mod.execute_operator_batch(str(repo), run_id)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_runner_cli")
