from __future__ import annotations

import pytest

from simplicio_loop.engine_boundary import EngineSelectionError, PythonLoopEngine, select_engine


def test_python_is_canonical_without_rust_or_runtime() -> None:
    receipt = select_engine("auto", rust_probe={"available": False, "reason_code": "missing"}, attempt_id="a")
    assert receipt.selected_engine == "python-loop"
    assert receipt.reason_code == "rust_unavailable_python_canonical"
    assert receipt.to_dict()["schema"] == "simplicio.loop-engine-selection/v1"
    assert PythonLoopEngine().execute(lambda: {"status": "ok"})["status"] == "ok"


def test_rust_requires_compatible_conformance_and_shadow_has_python_authority() -> None:
    with pytest.raises(EngineSelectionError, match="conformance"):
        select_engine("rust", rust_probe={"available": True, "compatible": False})
    shadow = select_engine("shadow", rust_probe={"available": True, "compatible": True,
                                                   "conformance_passed": True})
    assert shadow.selected_engine == "python-loop"
    assert shadow.reason_code == "shadow_read_only_python_authority"
    assert select_engine("auto", rust_probe={"available": True, "compatible": True,
                                               "conformance_passed": True}).selected_engine == "rust"
