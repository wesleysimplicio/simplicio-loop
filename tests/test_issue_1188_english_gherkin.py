from simplicio_loop.task_contract import compile_many, compile_task, validate_contract


ENGLISH_TASK = """System: Hermes Bot Mode
Feature: Preserve the roster
Type: Bug fix

1. Acceptance Criteria

Scenario 1: Cached roster remains visible
  Given a previous successful roster
  When a refresh fails
  Then the roster stays visible [AC1]
"""


def test_english_numbered_gherkin_compiles_with_scenarios():
    contract = compile_task(ENGLISH_TASK)
    assert contract["identity"]["system"] == "Hermes Bot Mode"
    assert contract["identity"]["feature"] == "Preserve the roster"
    assert contract["identity"]["type"] == "Bug fix"
    assert len(contract["scenarios"]) == 1
    scenario = contract["scenarios"][0]
    assert scenario["id"] == "SCN1"
    assert scenario["title"] == "Cached roster remains visible"
    assert scenario["given"] == ["a previous successful roster"]
    assert scenario["when"] == ["a refresh fails"]
    assert scenario["then"] == ["the roster stays visible [AC1]"]
    assert validate_contract(contract)["errors"] == []


def test_english_collection_does_not_report_empty_scenarios():
    payload = compile_many(ENGLISH_TASK)
    assert payload["task_count"] == 1
    assert payload["tasks"][0]["scenarios"]
    assert not any(
        "no scenarios parsed" in error
        for error in validate_contract(payload["tasks"][0])["errors"]
    )


def test_english_and_portuguese_tasks_split_independently():
    payload = compile_many(
        ENGLISH_TASK
        + "\n\nSistema: Loop\nFuncionalidade: Segundo\nTipo: Bug\n\n"
        + "1. Critérios de Aceite\nCenário 1: Compatível\nDado que x\nQuando y\nEntão z\n"
    )
    assert payload["task_count"] == 2
    assert payload["tasks"][0]["identity"]["system"] == "Hermes Bot Mode"
    assert payload["tasks"][1]["identity"]["system"] == "Loop"
    assert all(task["scenarios"] for task in payload["tasks"])


def test_missing_scenarios_name_english_and_portuguese_labels():
    contract = compile_task("System: Loop\nFeature: Empty\nType: Bug\n")
    errors = validate_contract(contract)["errors"]
    assert errors
    assert "Scenario N:" in errors[0]
    assert "Cenário N:" in errors[0]
    assert "1. Acceptance Criteria" in errors[0]
