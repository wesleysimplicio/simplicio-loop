"""Honest, portable progress rendering for a simplicio-loop run.

The progress protocol is deliberately independent of a terminal.  Runtimes can consume the
same ``simplicio.progress/v1`` JSON event, render compact text/Markdown, or opt into ANSI
animation.  A run never reports 100% merely because its phase is ``done``: completion must be
backed by a fresh, ready completion receipt from the oracle.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

SCHEMA = "simplicio.progress/v1"
PHASES = ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering", "done")
PHASE_META = {
    "intake": ("📥", "Contrato recebido"),
    "mapping": ("🗺️", "Contexto mapeado"),
    "planning": ("🧭", "Plano congelado"),
    "executing": ("⚙️", "Execução em andamento"),
    "validating": ("🧪", "Validação e evidências"),
    "watching": ("👁️", "Watcher verificando"),
    "delivering": ("📦", "Entrega reconciliada"),
    "done": ("✅", "Concluído pelo oracle"),
    "blocked": ("⛔", "Bloqueado"),
    "cancelled": ("🛑", "Cancelado"),
}
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _completion(run_dir: Path | None, state: Mapping[str, Any]) -> Dict[str, Any]:
    current = dict(state.get("completion") or {})
    if run_dir:
        path = run_dir / "completion-receipt.json"
        if path.is_file():
            try:
                current.update(_load_json(path))
            except (OSError, ValueError, TypeError):
                current["tag"] = "UNVERIFIED"
    return current


def _phase_percent(phase: str) -> int:
    if phase in PHASES:
        return round(PHASES.index(phase) / (len(PHASES) - 1) * 100)
    return 0


def build_progress(state: Mapping[str, Any], *, run_dir: str | Path | None = None,
                   frame: int = 0) -> Dict[str, Any]:
    """Build one deterministic progress event from ``state.json`` and its receipt.

    ``frame`` is only presentation metadata for an optional spinner. It never affects the
    percentage or completion verdict.
    """
    root = Path(run_dir) if run_dir else None
    phase = str(state.get("phase") or "intake").lower()
    completion = _completion(root, state)
    ready = bool(completion.get("ready")) and str(completion.get("verdict") or "").upper() in {"COMPLETE", "DRAINED"}
    evidence = dict(state.get("evidence") or {})
    watcher = dict(state.get("watcher") or {})
    evidence_ready = bool(evidence.get("ready")) and str(evidence.get("status") or "").upper() == "VERIFIED"
    watcher_ready = bool(watcher.get("ready")) or str(watcher.get("status") or "").upper() in {"MATCH", "VERIFIED"}
    percent = 100 if ready else min(99, _phase_percent(phase))
    icon, label = PHASE_META.get(phase, ("•", phase.replace("_", " ").title()))
    total = int(state.get("task_count") or 0)
    coverage = state.get("coverage") or {}
    verified = 0
    for item in coverage.values() if isinstance(coverage, Mapping) else ():
        if isinstance(item, Mapping):
            verified += int(item.get("verified") or 0)
    return {
        "schema": SCHEMA,
        "run_id": str(state.get("run_id") or ""),
        "phase": phase,
        "status": "COMPLETE" if ready else ("BLOCKED" if phase == "blocked" else "RUNNING"),
        "percent": percent,
        "icon": icon,
        "label": label,
        "spinner": SPINNER[frame % len(SPINNER)],
        "current_action": str(state.get("current_action") or ""),
        "next_action": str(state.get("next_action") or ""),
        "tasks": {"verified": verified, "total": total},
        "gates": {"evidence": evidence_ready, "watcher": watcher_ready, "oracle": ready},
        "completion": {"ready": ready, "verdict": str(completion.get("verdict") or "DELIVERY_PENDING"),
                       "reason_code": str(completion.get("reason_code") or "oracle_incomplete")},
    }


def render_text(event: Mapping[str, Any], *, width: int = 24) -> str:
    """Render a compact, Unicode-safe progress card for chat/LLM output."""
    pct = max(0, min(100, int(event.get("percent") or 0)))
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    gates = event.get("gates") or {}
    marks = " ".join(f"{'✅' if gates.get(k) else '▫️'} {k}" for k in ("evidence", "watcher", "oracle"))
    return (f"{event.get('icon', '•')} {event.get('label', 'Progresso')} · {pct:3d}%\n"
            f"[{bar}] {event.get('spinner', '·')}\n"
            f"{marks}\n"
            f"ação: {event.get('current_action') or 'aguardando'} → {event.get('next_action') or '—'}")


def render_markdown(event: Mapping[str, Any], *, width: int = 20) -> str:
    pct = max(0, min(100, int(event.get("percent") or 0)))
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"**{event.get('icon', '•')} {event.get('label', 'Progresso')} — {pct}%**\n\n`{bar}` · `{event.get('status', 'RUNNING')}`"


def render_ansi(event: Mapping[str, Any], *, width: int = 24) -> str:
    return "\x1b[2K\r" + render_text(event, width=width).replace("\n", "\x1b[2K\n")


def load_state(run_dir: str | Path) -> Dict[str, Any]:
    root = Path(run_dir)
    return _load_json(root / "state.json")


def stream(run_dir: str | Path, *, fmt: str = "text", interval: float = 0.25,
           once: bool = False, out: Any = None) -> Dict[str, Any]:
    """Print progress snapshots until terminal; ``once`` is safe for non-interactive hosts."""
    out = out or sys.stdout
    frame = 0
    root = Path(run_dir)
    while True:
        event = build_progress(load_state(root), run_dir=root, frame=frame)
        if fmt == "json":
            rendered = json.dumps(event, ensure_ascii=False, sort_keys=True)
        elif fmt == "markdown":
            rendered = render_markdown(event)
        elif fmt == "ansi":
            rendered = render_ansi(event)
        else:
            rendered = render_text(event)
        print(rendered, end="\n\n" if fmt != "ansi" else "", file=out, flush=True)
        if once or event["status"] in {"COMPLETE", "BLOCKED"}:
            return event
        frame += 1
        time.sleep(max(0.05, float(interval)))


__all__ = ["SCHEMA", "build_progress", "load_state", "render_ansi", "render_markdown", "render_text", "stream"]
