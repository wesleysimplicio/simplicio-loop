from pathlib import Path

from simplicio_loop import runner as runner_mod
from simplicio_loop.plan_contract import validate_plan


TASK_WITH_NODE_DEP = """Sistema: Hermes Bot Mode
Funcionalidade: Preserve the roster
Tipo: Bug

1. Critérios de Aceite

Cenário 1: Cached roster remains visible
  Dado que existe um roster válido
  Quando o refresh falha
  Então o roster permanece visível [RN01]

2. Regras de Negócio

RN01 – Preserve the last valid roster.

6. Dependências

Node.js native test runner only; no package dependency is required.
"""


def _mapper_payload(files):
    return {
        "handoff": {
            "stdout": {
                "context_pack": {
                    "pack_hash": "pack-1189",
                    "files": [{"path": path} for path in files],
                }
            }
        },
        "repo_state_before": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "one"},
        "repo_state_after": {"head": "abc", "tree_hash": "tree", "dirty_status_hash": "two"},
        "generated_at": "2026-08-14T00:00:00Z",
    }


def test_extract_repo_file_hints_ignores_node_js_dependency_name(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "plugin.js").write_text("export {}\n", encoding="utf-8")

    hints = runner_mod._extract_repo_file_hints(
        "Edit plugin.js.\n\n6. Dependencies\n\nNode.js native test runner only.",
        repo,
    )
    assert hints == ["plugin.js"]


def test_extract_repo_file_hints_ignores_bare_tech_name_outside_dependencies(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "plugin.js").write_text("export {}\n", encoding="utf-8")

    hints = runner_mod._extract_repo_file_hints(
        "Requires Node.js and Next.js. Change plugin.js only.",
        repo,
    )
    assert hints == ["plugin.js"]
    assert "Node.js" not in hints
    assert "Next.js" not in hints


def test_build_plan_does_not_require_node_js_as_source_target(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "plugin.js").write_text("export {}\n", encoding="utf-8")
    tasks = runner_mod.compile_many(TASK_WITH_NODE_DEP, source_path="task.md")["tasks"]
    plan = runner_mod._build_plan_with_hints(
        tasks,
        _mapper_payload(["plugin.js"]),
        repo,
        TASK_WITH_NODE_DEP,
    )
    targets = plan["steps"][0]["candidate_targets"]
    assert "plugin.js" in targets
    assert "Node.js" not in targets
    receipt = validate_plan(plan, tasks, repo, current_state=plan["repo_state"])
    assert receipt["valid"] is True, receipt["errors"]
    assert not any("Node.js" in error for error in receipt["errors"])
