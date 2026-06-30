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

Cross-agent handoff: an INCOMPLETE stop (budget halted, iteration cap, manual STOP
signal) writes `.orchestrator/loop/HANDOFF.md` before clearing the scratchpad, so a
different agent/runtime picking up this repo cold — because the first one ran out of
budget — can resume without re-deriving the goal, the verified acceptance criteria, or
the dead-end attempts. A successful (promise-fulfilled) stop needs no handoff.
"""
import json
import os
import re
import sys
import time

LOOP_DIR = os.path.join(".orchestrator", "loop")
SCRATCHPAD = os.path.join(LOOP_DIR, "scratchpad.md")
DONE_FLAG = os.path.join(LOOP_DIR, "done")
LAST_RESP = os.path.join(LOOP_DIR, "last_response.txt")
ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
JOURNAL = os.path.join(LOOP_DIR, "journal.jsonl")
HANDOFF = os.path.join(LOOP_DIR, "HANDOFF.md")
STOP_SIGNAL = os.path.join(".orchestrator", "STOP")
BUDGET = os.path.join(".orchestrator", "loop-budget.json")
GATE_LOCK = os.path.join(LOOP_DIR, "gate.lock")
GATE_TTL_SEC = 1800  # 30 min — a stale lock must NEVER permanently trap the loop (fail-open)
WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")
SPINDLE_STATE = os.path.join(LOOP_DIR, "spindle.json")

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
    for p in (SCRATCHPAD, DONE_FLAG, LAST_RESP, WATCHER_STATE):
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
        return (time.time() - os.path.getmtime(GATE_LOCK)) < GATE_TTL_SEC
    except Exception:
        return False


def read_anchor():
    """Return the parsed task anchor dict, or None if absent/corrupt. Fail-open."""
    try:
        with open(ANCHOR, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def anchor_pending():
    """Return the unverified acceptance-criteria ids from the task anchor, or [].

    The mechanical anti-drift gate: a `<promise>` must not end the loop while the frozen task anchor
    still has criteria that are not `done`. FAIL-OPEN: a missing / unreadable / empty anchor, or one
    with no criteria, returns [] so the gate never blocks — a buggy anchor must never trap the loop.
    """
    data = read_anchor()
    if not data:
        return []
    crit = data.get("criteria") or []
    return [c.get("id") for c in crit
            if isinstance(c, dict) and c.get("status") != "done"]


def tail_journal(n=8):
    """Last N attempt records from the journal, oldest first. [] on any read error."""
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def attempt_suffix(a):
    bits = []
    if a.get("execution_state"):
        bits.append("state=%s" % a["execution_state"])
    if a.get("stage_id"):
        bits.append("stage=%s" % a["stage_id"])
    if a.get("decision"):
        bits.append("decision=%s" % a["decision"])
    if a.get("validator"):
        bits.append("validator=%s" % a["validator"])
    if a.get("retry_count") is not None:
        bits.append("retry=%s" % a["retry_count"])
    if a.get("chunk_id"):
        bits.append("chunk=%s" % a["chunk_id"])
    if a.get("source_artifact"):
        bits.append("source=%s" % a["source_artifact"])
    if a.get("next_action"):
        bits.append("next=%s" % a["next_action"])
    if a.get("blocked_reason"):
        bits.append("blocked=%s" % a["blocked_reason"])
    return (" — " + " | ".join(bits)) if bits else ""


def write_handoff(reason, meta=None, body=None):
    """Write the cross-agent continuation artifact before an INCOMPLETE stop.

    Aggregates the frozen task anchor (goal + acceptance criteria + evidence), the last journal
    attempts (what was already tried, to avoid re-running a dead end), and the live scratchpad
    iteration/promise — everything a fresh agent needs to resume cold, without this conversation.
    Fail-open: any error here must never block the stop itself.
    """
    try:
        anchor = read_anchor() or {}
        criteria = anchor.get("criteria") or []
        attempts = tail_journal()
        lines = [
            "# simplicio-loop handoff",
            "",
            "Stop reason: %s" % reason,
            "Stopped at: %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ]
        if meta:
            lines += [
                "Iteration: %s / %s" % (meta.get("iteration", "?"), meta.get("max_iterations", "?")),
                "Completion promise: %s" % (meta.get("completion_promise") or "(none set)"),
            ]
        if anchor.get("goal"):
            lines += ["", "## Frozen goal", "", anchor["goal"]]
        elif body:
            lines += ["", "## Goal (from scratchpad, no anchor set)", "", body]
        if criteria:
            lines += ["", "## Acceptance criteria"]
            for c in criteria:
                if not isinstance(c, dict):
                    continue
                mark = "x" if c.get("status") == "done" else " "
                ev = (" — %s" % c["evidence"]) if c.get("evidence") else ""
                lines.append(
                    "- [%s] %s (%s)%s"
                    % (mark, c.get("text", c.get("id", "?")), c.get("status", "pending"), ev)
                )
        if attempts:
            lines += ["", "## Last attempts (`scripts/loop_journal.py resume` for the full read)"]
            for a in attempts:
                lines.append(
                    "- iter %s: %s -> %s (fp %s)%s%s"
                    % (
                        a.get("iteration", "?"),
                        a.get("action", "?"),
                        a.get("gate", "?"),
                        (a.get("fingerprint") or "")[:12],
                        (" — %s" % a["note"]) if a.get("note") else "",
                        attempt_suffix(a),
                    )
                )
        lines += [
            "",
            "## Resume",
            "",
            "1. `python3 scripts/task_anchor.py status` (or `gate --exit-code`) — verified vs open.",
            "2. `python3 scripts/loop_journal.py resume` — dead-end actions to avoid.",
            "3. `git log --oneline -10` / `git diff` — what already landed.",
            "4. Re-arm the loop once the stop cause (budget/cap/manual) is resolved.",
            "",
        ]
        tmp = HANDOFF + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, HANDOFF)
    except Exception:
        pass  # fail-open: a broken handoff write must never block the stop


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


def watcher_verify():
    """Run pre-promise watcher verification per Asolaria N-Nest Corrective Gate pattern.

    Reads `.orchestrator/loop/watcher_state.json` written by the watcher process (a separate
    agent/PID that independently re-executes the work and compares results against the agent's
    reported output). Gate: `reported == watcher.recomputed_truth`.

    Returns (passed: bool, tag: str) where tag is "MEASURED" (verified) or "UNVERIFIED" (not
    verified or mismatch). If no watcher state exists → UNVERIFIED (gate fails). Fail-open:
    a corrupt or missing watcher state NEVER traps the loop — it simply gates the promise.
    """
    try:
        if not os.path.exists(WATCHER_STATE):
            return False, "UNVERIFIED"
        with open(WATCHER_STATE, encoding="utf-8") as f:
            state = json.load(f)
        match = bool(state.get("match", False))
        status = str(state.get("status", "UNVERIFIED"))
        if match and status == "MEASURED":
            return True, "MEASURED"
        return False, "UNVERIFIED"
    except Exception:
        return False, "UNVERIFIED"


def spindle_latched():
    """True when a spindle handoff exists with an unreleased latch.

    A latched handoff means agent A handed off to agent B and is waiting for B to
    confirm receipt. When latched, the loop must NOT re-feed the goal — the handoff
    target will pick up. Fail-open: unreadable/missing file → False (never trap).
    """
    try:
        if not os.path.exists(SPINDLE_STATE):
            return False
        with open(SPINDLE_STATE, encoding="utf-8") as f:
            s = json.load(f)
        return bool(s.get("latch", False))
    except Exception:
        return False


def spindle_active():
    """True when a spindle handoff exists and IS confirmed (current agent is processing).

    An active (confirmed) handoff means the receiving agent confirmed receipt and is
    working. The loop can continue normally for this agent. Fail-open: unreadable/missing
    file → False (never trap).
    """
    try:
        if not os.path.exists(SPINDLE_STATE):
            return False
        with open(SPINDLE_STATE, encoding="utf-8") as f:
            s = json.load(f)
        return bool(s.get("current_agent")) and not bool(s.get("latch", False))
    except Exception:
        return False


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
        meta, body = None, None
        if os.path.exists(SCRATCHPAD):
            try:
                with open(SCRATCHPAD, encoding="utf-8") as f:
                    meta, body = parse_frontmatter(f.read())
            except OSError:
                meta, body = None, None

        # Explicit STOP signal beats everything — but still hand off if there was live state.
        if os.path.exists(STOP_SIGNAL):
            if meta is not None:
                write_handoff("manual STOP signal", meta, body)
            cleanup_and_stop()
        # Waiting on a background gate (workflow / CI / long task)? Let the turn end WITHOUT
        # consuming an iteration or re-feeding — we are blocked on that gate, not idle. The gate's
        # completion re-invokes the agent; the loop resumes then (lock is gone). Preserves state.
        if gate_running():
            allow_stop()
        # (1) No active loop.
        if not os.path.exists(SCRATCHPAD):
            allow_stop()
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

        # Pre-promise: watcher-gate — independent verification before any promise is honored.
        # Per Asolaria N-Nest Corrective Gate: each agent PID has a watcher PID that
        # independently re-computes the truth. Gate: reported == watcher.recomputed_truth.
        watcher_pass, watcher_tag = watcher_verify()

        # Completion detection (capture folded in for single-hook runtimes like Claude).
        if promise and resp:
            m = PROMISE_RE.search(resp)
            if m and m.group(1).strip() == promise.strip():
                has_evidence = bool(EVIDENCE_RE.search(resp))
                # The promise is honored only with evidence AND watcher verification AND no
                # acceptance criterion still open in the task anchor. The watcher-gate ensures
                # the agent's result was independently re-executed and matched before the
                # promise is accepted — corrective gate per Asolaria.
                if ((not evidence_required) or has_evidence) and watcher_pass and not anchor_pending():
                    cleanup_and_stop()  # (3) promise fulfilled → stop, no handoff needed
                # promise without evidence, or watcher disagrees, or anchor still has open ACs
                # → ignore, keep looping
        # (3') Cursor capture may have raised the flag.
        if os.path.exists(DONE_FLAG):
            cleanup_and_stop()
        # (4) Iteration cap — incomplete stop, hand off.
        if max_iter > 0 and iteration >= max_iter:
            write_handoff("max_iterations cap reached", meta, body)
            cleanup_and_stop()
        # (5) Budget halted — incomplete stop, hand off. This is the exact "ran out of tokens/$"
        # case: a different agent must be able to pick this up cold.
        if budget_halted():
            write_handoff("budget halted", meta, body)
            cleanup_and_stop()
        # (5b) Spindle handoff — latched handoff overrides re-feed.
        # A latched spindle means agent A handed off to agent B and is waiting for
        # B to confirm receipt. The loop must NOT re-feed the goal — the handoff
        # target will pick up from here. Just stop cleanly. Fail-open: if the spindle
        # state is unreadable, spindle_latched() returns False and we continue
        # normally, which is safe (the latched agent will still have the state file
        # on disk to resume from).
        if spindle_latched():
            # Attempt to read the next_agent name for the handoff message; fail-open.
            next_agent = "?"
            try:
                with open(SPINDLE_STATE, encoding="utf-8") as _f:
                    _s = json.load(_f)
                    next_agent = _s.get("next_agent", "?")
            except Exception:
                pass
            write_handoff("spindle handoff (latched — waiting for '%s')" % next_agent, meta, body)
            cleanup_and_stop()
        # (6) Continue: bump iteration in place, re-feed the goal body.
        nxt = iteration + 1
        with open(SCRATCHPAD, encoding="utf-8") as f:
            raw = f.read()
        new_content = re.sub(
            r"^iteration:\s*\d+", "iteration: %d" % nxt, raw, count=1, flags=re.M
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
        header = "[simplicio-loop iteration %d.%s%s %s]" % (nxt, promise_hint, ac_hint, watcher_tag)
        emit_refeed(header + "\n\n" + (body or ""))
    except Exception:
        allow_stop()  # fail-open, always


if __name__ == "__main__":
    main()
