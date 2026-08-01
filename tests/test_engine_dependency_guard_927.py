from __future__ import annotations

import pytest

from simplicio_loop.engine_dependency_guard import (
    assert_core_dependency_firewall,
    scan_core_imports,
)


def test_core_dependency_firewall_passes_current_package() -> None:
    assert_core_dependency_firewall("simplicio_loop")


def test_core_dependency_firewall_rejects_runtime_internal_import(tmp_path) -> None:
    package = tmp_path / "simplicio_loop"
    package.mkdir()
    (package / "bad.py").write_text(
        "from simplicio_runtime.internal.engine import Engine\n", encoding="utf-8"
    )

    violations = scan_core_imports(package)
    assert [(item.path, item.line, item.module) for item in violations] == [
        ("bad.py", 1, "simplicio_runtime.internal.engine")
    ]
    with pytest.raises(RuntimeError, match="dependency-firewall"):
        assert_core_dependency_firewall(package)
