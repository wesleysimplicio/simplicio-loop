"""Static dependency firewall for the canonical Python Loop engine."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "simplicio.loop-engine-dependency-firewall/v1"
FORBIDDEN_PREFIXES = (
    "simplicio_runtime",
    "simplicio_runtime_internal",
)


@dataclass(frozen=True)
class DependencyViolation:
    path: str
    line: int
    module: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "module": self.module}


def _module_name(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return "." * node.level + (node.module or "")
    return ""


def scan_core_imports(package_root: str | Path) -> tuple[DependencyViolation, ...]:
    """Return forbidden Runtime/internal imports found under one core package."""
    root = Path(package_root).resolve()
    if not root.is_dir():
        raise ValueError(f"core package does not exist: {root}")
    violations: list[DependencyViolation] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {"__pycache__", "_bundle"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ValueError(f"cannot parse core module {path}: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module = _module_name(node)
            if any(module == prefix or module.startswith(prefix + ".")
                   for prefix in FORBIDDEN_PREFIXES):
                violations.append(DependencyViolation(
                    path=path.relative_to(root).as_posix(), line=int(node.lineno), module=module,
                ))
    return tuple(violations)


def assert_core_dependency_firewall(package_root: str | Path) -> None:
    violations = scan_core_imports(package_root)
    if violations:
        details = "; ".join(
            f"{item.path}:{item.line} imports {item.module}" for item in violations
        )
        raise RuntimeError(f"{SCHEMA} violation: {details}")


__all__ = [
    "SCHEMA", "FORBIDDEN_PREFIXES", "DependencyViolation",
    "scan_core_imports", "assert_core_dependency_firewall",
]
