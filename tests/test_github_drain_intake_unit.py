from __future__ import annotations

import pytest

from simplicio_loop.github_drain_intake import (
    DrainIntentError,
    DrainPlanError,
    classify_issue_risk,
    extract_issue_dependencies,
    parse_natural_drain_request,
    plan_issue_waves,
)


@pytest.mark.parametrize(
    ("phrase", "repository", "language"),
    [
        ("termine todas as issues do projeto acme/widgets", "acme/widgets", "pt-BR"),
        ("finalize todos os tickets de acme/widgets", "acme/widgets", "pt-BR"),
        ("conclua todas as tarefas em https://github.com/acme/widgets", "acme/widgets", "pt-BR"),
        ("finish all open issues in project acme/widgets", "acme/widgets", "en"),
        ("complete all issues for https://github.com/acme/widgets.git", "acme/widgets", "en"),
        ("/simplicio-loop drain all tickets in simplicio/loop", "simplicio/loop", "en"),
    ],
)
def test_natural_parser_accepts_strict_pt_br_and_en(phrase, repository, language):
    intent = parse_natural_drain_request(phrase)
    assert intent.repository == repository
    assert intent.language == language
    assert intent.owner == repository.split("/")[0]
    assert intent.repo == repository.split("/")[1]
    assert intent.to_dict()["scope"] == "all_open_issues"


@pytest.mark.parametrize(
    ("phrase", "reason"),
    [
        ("", "empty_request"),
        ("list all issues in acme/widgets", "completion_intent_missing"),
        ("finish issues in acme/widgets", "all_scope_missing"),
        ("finish all issue in acme/widgets", "issue_scope_missing"),
        ("finish all issue #7 in acme/widgets", "scope_narrowed"),
        ("finish all issues except #7 in acme/widgets", "scope_narrowed"),
        ("finish all issues in https://github.com/acme/widgets/issues/7", "scope_narrowed"),
        ("termine todas as issues menos #8 em acme/widgets", "scope_narrowed"),
        ("finish all issues in project Widgets", "repository_missing"),
        ("finish all issues in acme/widgets and acme/other", "repository_ambiguous"),
    ],
)
def test_natural_parser_rejects_ambiguous_or_narrowed_scope(phrase, reason):
    with pytest.raises(DrainIntentError) as excinfo:
        parse_natural_drain_request(phrase)
    assert excinfo.value.reason_code == reason


def test_dependency_parser_accepts_only_explicit_internal_markdown_fields():
    body = """
Depends on #1 and #2
Does not depend on #99
> Blocked by #98
Blocked by acme/other#97

```
Requires #96
```

<!-- Depends on #95 -->
<!--
Depends on #93
-->

Requires not #92

## Dependencies
- #3
2. #4 and #5
- does not depend on #12
- não depende de #13
- acme/other#14
- https://github.com/acme/other/issues/15

## Dependencies / integrations
- #94
"""
    assert extract_issue_dependencies(body) == [1, 2, 3, 4, 5]


def test_same_repository_repeated_in_request_is_not_false_ambiguity():
    intent = parse_natural_drain_request(
        "finish all issues in acme/widgets from https://github.com/acme/widgets"
    )
    assert intent.repository == "acme/widgets"


def test_risk_and_dependency_waves_are_deterministic():
    assert classify_issue_risk("[P0] security migration") == "high"
    assert classify_issue_risk("queue tuning", ["performance"]) == "medium"
    assert classify_issue_risk("copy update") == "low"
    items = {
        "1": {"state": "planned", "dependencies": [], "risk": "low"},
        "2": {"state": "planned", "dependencies": [], "risk": "high"},
        "3": {"state": "planned", "dependencies": [1, 2], "risk": "medium"},
        "4": {"state": "remote_closed", "dependencies": [], "risk": "high"},
        "5": {
            "state": "planned", "dependencies": [77], "external_dependencies_closed": [77],
            "risk": "low",
        },
    }
    assert plan_issue_waves(items)["waves"] == [
        {"index": 1, "issues": [2, 1, 5], "risk_order": ["high", "low", "low"]},
        {"index": 2, "issues": [3], "risk_order": ["medium"]},
    ]


def test_wave_planner_rejects_unknown_dependencies_and_cycles():
    with pytest.raises(DrainPlanError) as unknown:
        plan_issue_waves({"1": {"state": "planned", "dependencies": [77], "risk": "low"}})
    assert unknown.value.reason_code == "dependency_unresolved"

    with pytest.raises(DrainPlanError) as cycle:
        plan_issue_waves({
            "1": {"state": "planned", "dependencies": [2], "risk": "low"},
            "2": {"state": "planned", "dependencies": [1], "risk": "low"},
        })
    assert cycle.value.reason_code == "dependency_cycle"
