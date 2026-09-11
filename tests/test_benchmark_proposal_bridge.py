import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("proposal_bridge", Path(__file__).parents[1] / "bench/proposal_to_edit_plan.py")
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


def response(files, finish="stop"):
    return {"status": "response_received", "response": {"choices": [
        {"finish_reason": finish, "message": {"content": json.dumps({"files": files})}}
    ]}}


def test_exact_replacement_and_no_application(tmp_path):
    target = tmp_path / "total.py"
    target.write_text("def total(v): return len(v)\n")
    plan = _bridge.compile_plan(response({"total.py": "def total(v): return sum(v)\n"}), tmp_path, ["total.py"])
    assert plan["file"] == "total.py"
    assert plan["operations"][0]["find"] == target.read_text()
    assert "len(v)" in target.read_text()


def test_scope_escape_rejected(tmp_path):
    with pytest.raises(ValueError, match="write set"):
        _bridge.compile_plan(response({"other.py": "x = 1"}), tmp_path, ["total.py"])


def test_incomplete_response_rejected(tmp_path):
    with pytest.raises(ValueError, match="incomplete"):
        _bridge.compile_plan(response({"total.py": "x = 1"}, "length"), tmp_path, ["total.py"])


def test_invalid_python_rejected(tmp_path):
    with pytest.raises(SyntaxError):
        _bridge.compile_plan(response({"total.py": "def broken("}), tmp_path, ["total.py"])
