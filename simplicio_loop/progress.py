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
def _ascii(value: Any) -> str:
    """Replace presentation glyphs; payloads remain Unicode-safe and unchanged."""
    text = str(value or "")
    for glyph, replacement in (("📥", "[in]"), ("🗺️", "[map]"), ("🧭", "[plan]"),
                               ("⚙️", "[run]"), ("🧪", "[test]"), ("👁️", "[watch]"),
                               ("📦", "[ship]"), ("✅", "[ok]"), ("⛔", "[blocked]"),
                               ("🛑", "[stop]"), ("█", "#"), ("░", "."),
                               ("⠋", "|"), ("⠙", "/"), ("⠹", "-"), ("⠸", "\\"),
                               ("▫️", "[ ]")):
        text = text.replace(glyph, replacement)
    return text


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
    # Receipts are authoritative when state.json has not yet been refreshed by a hook.
    if root:
        for path, target in ((root / "evidence-receipt.json", evidence),
                             (root / "loop" / "watcher_state.json", watcher)):
            if path.is_file():
                try:
                    receipt = _load_json(path)
                    target.update(receipt)
                    # Older hooks persisted only a status; promote that measured receipt
                    # over stale state.json flags without weakening the oracle gate.
                    if path.name == "evidence-receipt.json" and str(receipt.get("status") or "").upper() == "VERIFIED":
                        target["ready"] = True
                except (OSError, ValueError, TypeError):
                    target["status"] = "UNVERIFIED"
    evidence_ready = str(evidence.get("status") or "").upper() == "VERIFIED" and (
        bool(evidence.get("ready")) or "ready" not in evidence)
    watcher_status = str(watcher.get("status") or "").upper()
    watcher_ready = bool(watcher.get("ready")) or watcher_status in {"MATCH", "VERIFIED"} or (
        watcher_status == "MEASURED" and watcher.get("match") is True)
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
        # Optional lanes/events are copied as data, never used to infer completion. This keeps
        # the same JSON contract useful for fan-out dashboards and chat adapters.
        "lanes": _normalise_lanes(state.get("lanes")),
        "events": _normalise_events(state.get("events") or state.get("phase_events")),
    }


def _normalise_lanes(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    lanes = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        try:
            percent = int(item.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0
        lanes.append({"id": str(item.get("id") or item.get("lane") or f"lane-{index + 1}"),
                      "status": str(item.get("status") or "UNKNOWN").upper(),
                      "percent": max(0, min(100, percent)),
                      "worktree": str(item.get("worktree") or ""),
                      "action": str(item.get("action") or item.get("current_action") or "")})
    return lanes


def _normalise_events(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    events = []
    for item in value[-12:]:
        if not isinstance(item, Mapping):
            continue
        events.append({"phase": str(item.get("phase") or item.get("event") or "unknown"),
                       "status": str(item.get("status") or "INFO").upper(),
                       "task_id": str(item.get("task_id") or item.get("work_item_id") or ""),
                       "message": str(item.get("message") or item.get("reason_code") or "")})
    return events


def render_text(event: Mapping[str, Any], *, width: int = 24, ascii_only: bool = False) -> str:
    """Render a compact, Unicode-safe progress card for chat/LLM output."""
    pct = max(0, min(100, int(event.get("percent") or 0)))
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    gates = event.get("gates") or {}
    marks = " ".join(f"{'✅' if gates.get(k) else '▫️'} {k}" for k in ("evidence", "watcher", "oracle"))
    lines = [f"{event.get('icon', '•')} {event.get('label', 'Progresso')} · {pct:3d}%",
             f"[{bar}] {event.get('spinner', '·')}", marks,
             f"ação: {event.get('current_action') or 'aguardando'} → {event.get('next_action') or '—'}"]
    lanes = event.get("lanes") or []
    if lanes:
        lines.append("lanes: " + " · ".join(f"{x['id']} {x['percent']}%/{x['status']}" for x in lanes))
    events = event.get("events") or []
    if events:
        recent = events[-3:]
        lines.append("etapas: " + " · ".join(
            f"{x['phase']}" + (f"[{x['status']}]" if x.get("status") else "") for x in recent))
    if ascii_only:
        lines = [_ascii(line) for line in lines]
    return "\n".join(lines)


def render_markdown(event: Mapping[str, Any], *, width: int = 20, ascii_only: bool = False) -> str:
    pct = max(0, min(100, int(event.get("percent") or 0)))
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    rendered = f"**{event.get('icon', '•')} {event.get('label', 'Progresso')} — {pct}%**\n\n`{bar}` · `{event.get('status', 'RUNNING')}`"
    if (event.get("lanes") or []):
        rendered += "\n\nLanes: " + ", ".join(f"`{x['id']}` {x['percent']}% ({x['status']})" for x in event["lanes"])
    return _ascii(rendered) if ascii_only else rendered


def render_ansi(event: Mapping[str, Any], *, width: int = 24) -> str:
    return "\x1b[2K\r" + render_text(event, width=width).replace("\n", "\x1b[2K\n")


def load_state(run_dir: str | Path) -> Dict[str, Any]:
    root = Path(run_dir)
    return _load_json(root / "state.json")


def stream(run_dir: str | Path, *, fmt: str = "text", interval: float = 0.25,
           once: bool = False, out: Any = None, no_animation: bool = False,
           ascii_only: bool = False) -> Dict[str, Any]:
    """Print progress snapshots until terminal; ``once`` is safe for non-interactive hosts."""
    out = out or sys.stdout
    frame = 0
    root = Path(run_dir)
    while True:
        event = build_progress(load_state(root), run_dir=root, frame=frame)
        if fmt == "json":
            rendered = json.dumps(event, ensure_ascii=False, sort_keys=True)
        elif fmt == "markdown":
            rendered = render_markdown(event, ascii_only=ascii_only)
        elif fmt == "ansi":
            rendered = render_text(event, ascii_only=ascii_only) if no_animation else render_ansi(event)
        else:
            rendered = render_text(event, ascii_only=ascii_only)
        print(rendered, end="\n\n" if fmt != "ansi" else "", file=out, flush=True)
        if once or no_animation or event["status"] in {"COMPLETE", "BLOCKED"}:
            return event
        frame += 1
        time.sleep(max(0.05, float(interval)))


__all__ = ["SCHEMA", "build_progress", "load_state", "render_ansi", "render_markdown", "render_text", "stream"]
