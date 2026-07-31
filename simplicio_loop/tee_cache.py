"""Content-addressed, local-only tee cache for reversible CLI output."""
from __future__ import annotations

import hashlib
from pathlib import Path


def write(root: str | Path, content: str) -> Path:
    base = Path(root).resolve() / ".orchestrator" / "tee"
    base.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest()
    path = base / f"{digest}.out"
    if not path.exists():
        path.write_text(content, encoding="utf-8", errors="surrogateescape")
    return path


def retrieve(path: str | Path, root: str | Path = ".") -> str:
    base = (Path(root).resolve() / ".orchestrator" / "tee").resolve()
    candidate = Path(path).resolve()
    if candidate.parent != base or candidate.suffix != ".out":
        raise ValueError("tee path must be a direct .out file inside .orchestrator/tee")
    content = candidate.read_text(encoding="utf-8", errors="surrogateescape")
    expected = candidate.stem
    actual = hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest()
    if actual != expected:
        raise ValueError("tee content digest mismatch")
    return content
