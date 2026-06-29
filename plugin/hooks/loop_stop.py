#!/usr/bin/env python3
"""simplicio-loop — stop hook (cross-runtime, cross-platform).

Fires when an agent turn ends. Decides whether to RE-FEED the goal (continue the
Ralph loop) or let the agent STOP. Works under Claude Code (Stop hook) and Cursor
(stop hook); detects the runtime from env and emits the matching control object.

SAFETY: fail-open. On ANY error, ambiguity, or missing state, ALLOW STOP — a buggy
hook must never trap the agent in an endless loop. The real guards are the
`max_iterations` cap and the $ budget kill-switch, never this script's cleverness.

State (single source of truth): .orchestrator/loop/scratchpad.md  (+ sibling `done` flag)
Reads stdin JSON from the host (Claude: {transcript_path,...}; Cursor: {text,...}).
"""
import json
import os
import re
import sys

LOOP_DIR = os.path.join(".orchestrator", "loop")
SCRATCHPAD = os.path.join(LOOP_DIR, "scratchpad.md")
DONE_FLAG = os.path.join(LOOP_DIR, "done")
LAST_RESP = os.path.join(LOOP_DIR, "last_response.txt")
ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
STOP_SIGNAL = os.path.join(".orchestrator", "STOP")
BUDGET = os.path.join(".orchestrator", "loop-budget.json")
GATE_LOCK = os.path.join(LOOP_DIR, "gate.lock")
GATE_TTL_SEC = 1800  # 30 min — a stale lock must NEVER permanently trap the loop (fail-open)

EVIDENCE_RE = re.compile(
    r"(https?://\S+/pull/\d+)"          # a PR URL
    r"|(\b(pass|passed|passing|green|ok)\b)"  # a gate verdict
    r"|([\w./-]+:\d+)"                   # a file:line receipt
    r"|([✓✅])",
    re.IGNORECASE,
)
PROMISE_RE = re.compile(r"<promise>\s*(.*?)\s*</promise>", re.IGNORECASE | re.DOTALL)


def allow_stop():
    """Emit nothing actionable → the agent is allowed to stop. Always exit 0."""
    sys.exit(0)


def cleanup_and_stop():
    for p in (SCRATCHPAD, DONE_FLAG, LAST_RESP):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    allow_stop()


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def parse_frontmatter(text):
    """Return (meta dict, body str) or (None, None) on corruption."""
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    meta = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta, parts[2].strip()


def last_assistant_text(stdin):
    # Cursor passes the response text inline.
    if isinstance(stdin.get("text"), str):
        return stdin["text"]
    # Cursor capture hook may have stashed it.
    if os.path.exists(LAST_RESP):
        try:
            with open(LAST_RESP, encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    # Claude passes a transcript path (JSONL); read the last assistant message.
    tp = stdin.get("transcript_path")
    if tp and os.path.exists(tp):
        try:
            txt = ""
            with open(tp, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("role") == "assistant" or ev.get("type") == "assistant":
                        msg = ev.get("message", ev)
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        txt = content or txt
            return txt
        except OSError:
            return ""
    return ""


def gate_running():
    """True when a background gate (verification workflow / CI / long task) is in flight + fresh.

    The orchestrator touches `.orchestrator/loop/gate.lock` before launching a background gate and
    removes it on completion. While present AND fresh, the turn ended because we are WAITING on that
    gate — not because the loop is idle — so the Stop hook must NOT re-feed the goal. A stale lock
    (older than the TTL) is ignored so a leftover file can never trap the agent (fail-open).
    """
    try:
        if not os.path.exists(GATE_LOCK):
            return False
        import time
        return (time.time() - os.path.getmtime(GATE_LOCK)) < GATE_TTL_SEC
    except Exception:
        return False


def anchor_pending():
    """Return the unverified acceptance-criteria ids from the task anchor, or [].

    The mechanical anti-drift gate: a `<promise>` must not end the loop while the frozen task anchor
    still has criteria that are not `done`. Read the anchor JSON DIRECTLY (no dependency on
    `scripts/task_anchor.py`, which the lean marketplace plugin does not ship — the hook must stay
    self-contained). FAIL-OPEN: a missing / unreadable / empty anchor, or one with no criteria,
    returns [] so the gate never blocks — a buggy anchor must never trap the loop, and the rejection
    it does cause is still bounded by `max_iterations` + the budget. Only a cleanly-parsed anchor
    with ≥1 criterion that is not `done` reports pending.
    """
    try:
        with open(ANCHOR, encoding="utf-8") as f:
            data = json.load(f)
        crit = data.get("criteria") or []
        return [c.get("id") for c in crit
                if isinstance(c, dict) and c.get("status") != "done"]
    except Exception:
        return []  # fail-open: anchor unreadable ≠ trap


def budget_halted():
    try:
        if not os.path.exists(BUDGET):
            return False
        with open(BUDGET, encoding="utf-8") as f:
            b = json.load(f)
        if str(b.get("state", "")).lower() == "halted":
            return True
        ceiling = float(b.get("daily_usd_ceiling", 0) or 0)
        spent = float(b.get("spent_usd_today", 0) or 0)
        return ceiling > 0 and spent >= ceiling
    except Exception:
        return False  # fail-open: budget unreadable ≠ trap


def emit_refeed(followup):
    """Emit the re-feed in BOTH schemas; each runtime reads its own key."""
    out = {
        "followup_message": followup,            # Cursor
        "decision": "block",                      # Claude Code Stop hook
        "reason": followup,
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def main():
    try:
        # Explicit STOP signal beats everything.
        if os.path.exists(STOP_SIGNAL):
            cleanup_and_stop()
        # Waiting on a background gate (workflow / CI / long task)? Let the turn end WITHOUT
        # consuming an iteration or re-feeding — we are blocked on that gate, not idle. The gate's
        # completion re-invokes the agent; the loop resumes then (lock is gone). Preserves state.
        if gate_running():
            allow_stop()
        # (1) No active loop.
        if not os.path.exists(SCRATCHPAD):
            allow_stop()
        with open(SCRATCHPAD, encoding="utf-8") as f:
            content = f.read()
        meta, body = parse_frontmatter(content)
        # (2) Corrupt state.
        if meta is None:
            cleanup_and_stop()
        try:
            iteration = int(meta.get("iteration", "1"))
            max_iter = int(meta.get("max_iterations", "0"))
        except ValueError:
            cleanup_and_stop()
        promise = meta.get("completion_promise", "null")
        promise = None if promise in (None, "null", "") else promise
        evidence_required = str(meta.get("evidence_required", "true")).lower() != "false"

        stdin = read_stdin_json()
        resp = last_assistant_text(stdin)

        # Completion detection (capture folded in for single-hook runtimes like Claude).
        if promise and resp:
            m = PROMISE_RE.search(resp)
            if m and m.group(1).strip() == promise.strip():
                has_evidence = bool(EVIDENCE_RE.search(resp))
                # The promise is honored only with evidence AND no acceptance criterion still open
                # in the task anchor — the mechanical anti-drift gate. Pending ACs ⇒ ignore the
                # promise and keep looping (still bounded by max_iter), never a false "done".
                if ((not evidence_required) or has_evidence) and not anchor_pending():
                    cleanup_and_stop()  # (3) promise fulfilled → stop
                # promise without evidence, or anchor still has open ACs → ignore, keep looping
        # (3') Cursor capture may have raised the flag.
        if os.path.exists(DONE_FLAG):
            cleanup_and_stop()
        # (4) Iteration cap.
        if max_iter > 0 and iteration >= max_iter:
            cleanup_and_stop()
        # (5) Budget halted.
        if budget_halted():
            cleanup_and_stop()
        # (6) Continue: bump iteration in place, re-feed the goal body.
        nxt = iteration + 1
        new_content = re.sub(
            r"^iteration:\s*\d+", "iteration: %d" % nxt, content, count=1, flags=re.M
        )
        try:
            tmp = SCRATCHPAD + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp, SCRATCHPAD)
        except OSError:
            allow_stop()  # can't persist progress → don't risk an unbounded loop
        promise_hint = (
            " To finish: output <promise>%s</promise> ONLY when genuinely true AND "
            "backed by a passing gate." % promise
            if promise
            else ""
        )
        # Surface the still-open acceptance criteria so the next turn knows exactly what blocks
        # "done" — the anchor gate is why a promise would be ignored, so name the gap.
        pending = anchor_pending()
        ac_hint = (
            " Open acceptance criteria (verify each before the promise): %s."
            % ", ".join(p for p in pending if p)
            if pending
            else ""
        )
        header = "[simplicio-loop iteration %d.%s%s]" % (nxt, promise_hint, ac_hint)
        emit_refeed(header + "\n\n" + (body or ""))
    except Exception:
        allow_stop()  # fail-open, always


if __name__ == "__main__":
    main()
