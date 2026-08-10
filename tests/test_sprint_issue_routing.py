"""Regression coverage for real open simplicio-sprint issue shapes."""

import pytest

from simplicio_loop.route import route


@pytest.mark.parametrize(
    ("issue_number", "task", "expected_intent"),
    [
        (
            314,
            "[Binary formats] Migrate internal JSON state to HBP, HBI and TOML\n"
            "Implement legacy migration with dry-run, atomic write, rollback and package scans.",
            "mutate",
        ),
        (
            315,
            "[Quality] Enforce no-internal-JSON and prove binary-format migration E2E\n"
            "Make the release-blocking scanner and restart/recovery test matrix pass.",
            "validate",
        ),
        (
            306,
            "[Performance] Paralelizar conectores e execução de sprints com AnyIO, limites globais e Loop Hub\n"
            "Use structured concurrency, bounded fan-out, DAG scheduling, locks and retries.",
            "orchestrate",
        ),
        (
            319,
            "[Fast V3] Fixar generation por card/PR e reutilizar base entre tentativas\n"
            "Reuse Fast generations through Loop and keep attempts auditable.",
            "orchestrate",
        ),
        (
            320,
            "[P0][Architecture] Portfolio Intake Agent delegando lifecycle e completion ao Loop\n"
            "Sprint delegates lifecycle, retry, quality and completion to Loop.",
            "orchestrate",
        ),
    ],
)
def test_real_sprint_issue_shapes_route_to_expected_intent(
    issue_number: int, task: str, expected_intent: str
) -> None:
    result = route(task)
    assert result["intent"] == expected_intent, issue_number
    assert result["language"] == "en"
    assert result["instruction_language"] == "en"
    assert result["unresolved"] == []


@pytest.mark.parametrize(
    ("issue_number", "task", "expected_intent"),
    [
        (
            317,
            "[META-AUDIT] Revisar todas as issues — objetivos, fluxo de testes e critérios de aceite\n"
            "Review every issue, group work, control parallelism, and record evidence.",
            "orchestrate",
        ),
        (
            312,
            "plan: expandir Hypothesis pros 3 adapters completos + fixtures reais + contract tests de schema\n"
            "Add property tests, real fixtures, and schema contract tests.",
            "validate",
        ),
        (
            311,
            "proposta: teste por propriedade/fuzzing + fixtures com dados reais + revisão por invariante\n"
            "Exercise normalization invariants with fuzzing and integration fixtures.",
            "validate",
        ),
        (
            309,
            "feat(token-reflection): estender ledger do sprint com estimativa tiktoken e decisão de fan-out\n"
            "Use the ledger to recommend bounded fan-out across future attempts.",
            "orchestrate",
        ),
        (
            307,
            "[Integration] Compartilhar mapa canônico com overlays isolados nas execuções multi-worktree\n"
            "Share one canonical map across parallel worktrees and coalesce builds.",
            "orchestrate",
        ),
    ],
)
def test_second_real_sprint_batch_routes_to_expected_intent(
    issue_number: int, task: str, expected_intent: str
) -> None:
    result = route(task)
    assert result["intent"] == expected_intent, issue_number
    assert result["language"] == "en"
    assert result["instruction_language"] == "en"
    assert result["unresolved"] == []
