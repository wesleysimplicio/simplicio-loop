"""#559: `run --help` must enumerate accepted --delivery values, and an invalid value must
fail cleanly (argparse usage/error), never a raw Python traceback."""
from __future__ import annotations

import re

import pytest

from simplicio_loop import cli
from simplicio_loop.delivery import DELIVERY_ORDER


def test_run_help_enumerates_delivery_choices(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for choice in DELIVERY_ORDER[1:]:
        assert choice in out
    assert "implemented" in out


def test_run_accepts_valid_delivery_value(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "run", lambda repo, task_path, delivery, max_iterations: captured.update(
        delivery=delivery) or 0)
    rc = cli.main(["run", "--task", "t.md", "--delivery", "implemented"])
    assert rc == 0
    assert captured["delivery"] == "implemented"


def test_run_rejects_invalid_delivery_value_without_traceback(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--task", "t.md", "--delivery", "local"])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "invalid choice: 'local'" in captured.err
    for choice in DELIVERY_ORDER[1:]:
        assert choice in captured.err
    assert "implemented" in captured.err


def test_run_invalid_delivery_message_suggests_implemented_for_local(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--task", "t.md", "--delivery", "local"])
    captured = capsys.readouterr()
    assert re.search(r"implemented", captured.err)
