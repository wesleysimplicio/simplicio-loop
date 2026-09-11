"""Keep help discovery distinct from execution and option permutations."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "benchmark_help_discovery", Path(__file__).parents[1] / "bench/cli_help_coverage.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


def test_nested_commands_are_discovered():
    assert _module.children("positional arguments:\n  {status,drain}\n    status Show status\n") == ["status", "drain"]


def test_option_choices_are_not_commands():
    assert _module.children("options:\n  --route {legacy,mapper}\n") == []


def test_scalar_positionals_are_not_commands():
    assert _module.children("positional arguments:\n  run_id\noptions:\n --mode {a,b}\n") == []
