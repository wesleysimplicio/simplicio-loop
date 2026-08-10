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
