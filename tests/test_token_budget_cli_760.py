import json

from simplicio_loop.cli import main


def test_budget_cli_emits_receipt_without_local_llm(capsys):
    assert main([
        "budget", "receipt",
        "--run-id", "cli-760", "--total-tokens", "100",
        "--per-attempt-tokens", "50", "--spent-tokens", "20",
        "--reason-code", "initial",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "simplicio.loop.token-budget-receipt/v1"
    assert payload["remaining"]["tokens"] == 80
    assert payload["local_llm_started"] is False
