#!/usr/bin/env python3
"""Claude plugin UserPromptSubmit entrypoint for canonical Prompt enrichment.

The marketplace caches the complete repository and points CLAUDE_PLUGIN_ROOT at
`plugin/`, so the canonical host adapter remains the single implementation. If
a host copies only the plugin subtree, failure is explicit in additionalContext
instead of silently claiming Runtime/Prompt integration.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "simplicio.prompt-enrichment-receipt/v1"


def _repo_root() -> Path:
    plugin_root = Path(
        os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1]
    ).resolve()
    return plugin_root.parent


def _fallback(event: dict[str, Any], reason: str) -> dict[str, Any]:
    prompt = str(event.get("prompt") or event.get("user_prompt") or "")
    digest = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "adapter_version": "simplicio-loop-plugin-bootstrap/1",
        "profile": "mandatory",
        "runtime_status": "unavailable",
        "route_decision": None,
        "selected_handles": [],
        "selected_digests": [],
        "fallback": {
            "used": True,
            "reason_code": reason,
            "visible": True,
            "profile": "mandatory",
        },
        "tokens_before": 0 if not prompt else max(1, (len(prompt) + 3) // 4),
        "tokens_after": 0 if not prompt else max(1, (len(prompt) + 3) // 4),
        "bytes_before": len(prompt.encode("utf-8")),
        "bytes_after": len(prompt.encode("utf-8")),
        "enrichment_digest": digest,
        "authority": {"writes": False, "effects": False},
        "cache": {"hit": False},
        "session_id": event.get("session_id") or event.get("sessionId"),
    }
    context = (
        f"<!-- {RECEIPT_SCHEMA}\n"
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n-->"
    )
    return {
        "decision": "continue",
        "reason": "prompt_enrichment_degraded",
        "prompt_enrichment": receipt,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    }


def main() -> int:
    raw = sys.stdin.read() or "{}"
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    event.setdefault("hook_event_name", "UserPromptSubmit")

    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from adapters.claude.adapter import decide
    except (ImportError, ModuleNotFoundError) as error:
        result = _fallback(event, "loop_adapter_unavailable:" + type(error).__name__)
    else:
        try:
            result = decide(event)
        except Exception as error:  # Hook boundary must remain observable and bounded.
            result = _fallback(event, "loop_adapter_error:" + type(error).__name__)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("decision") != "block" else 2


if __name__ == "__main__":
    raise SystemExit(main())
