from simplicio_loop.task_contract import compile_task, validate_contract


HEADING_TASK = """Sistema: Loop
Funcionalidade: Heading parser
Tipo: Evolução

COMO operador,
QUERO compilar Markdown moderno,
PARA executar tasks válidas.

## Critérios de Aceite

### SCN1 — Intake
DADO que existe uma fila [RN01]
QUANDO o plano é criado
ENTÃO a fila é congelada

### Scenario 2: Execute
Given a valid plan
When the worker runs
Then it produces a receipt [RN02]

## Regras de Negócio
RN01 – O backlog é congelado.
RN02 – O worker produz receipt.
"""


def test_heading_based_task_compiles_as_one_task_with_scenarios():
    contract = compile_task(HEADING_TASK)
    assert len(contract["scenarios"]) == 2
    assert [item["id"] for item in contract["scenarios"]] == ["SCN1", "SCN2"]
    assert not validate_contract(contract)["errors"]


def test_legacy_numbered_task_remains_supported():
    contract = compile_task(
        "Sistema: Loop\nFuncionalidade: Legacy\nTipo: Bug\n\n"
        "1. Critérios de Aceite\nCenário 1: Compatível\nDado que x\nQuando y\nEntão z [RN01]\n\n"
        "2. Regras de Negócio\nRN01 – Regra.\n"
    )
    assert len(contract["scenarios"]) == 1
    assert not validate_contract(contract)["errors"]
