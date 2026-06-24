#!/usr/bin/env python3
"""simplicio-loop — run-journal + stall/progress detector (the loop's working memory).

The two highest-leverage upgrades to a loop orchestrator, made runnable. The classic re-feed loop
remembers nothing between turns except the git tree — so it can (a) re-derive the same triage every
turn and (b) OSCILLATE: try X, fail, try X again, forever, until the cap burns. This worker gives
the loop an explicit, durable **attempt memory** and a **stall detector** so it changes strategy or
escalates instead of re-feeding the same goal into the same failure.

It is deterministic and model-free — the fingerprint + stall math never call an LLM, so a resume is
reproducible from the on-disk journal (same discipline as `savings_harness`/`billing_aggregator`).

State: `.orchestrator/loop/journal.jsonl` — one append-only record per attempt:
    {"iteration", "action", "hypothesis", "gate": "pass|fail|blocked",
     "fingerprint": "<stable hash of the failure signature>", "note", "ts"}

Verbs:
  record      Append one attempt. Pass --gate pass|fail|blocked and (on fail) the gate output via
              --gate-output FILE or stdin; the failure FINGERPRINT is computed deterministically
              (line-numbers / paths / hex / timestamps normalized away) so the SAME failure hashes
              the SAME across turns.
  fingerprint Print the stable fingerprint of a failure text (FILE or stdin). Standalone helper.
  stall       Read the journal → verdict PROGRESS | STALLED. STALLED when the last K consecutive
              attempts all failed with the SAME fingerprint (default K=3). Prints the recommended
              action (switch-strategy | escalate) and the dead-end actions to avoid. Exit 10 when
              stalled (for `if:` gating), 0 otherwise — unless --exit-code is omitted (always 0).
  resume      The anti-oscillation read: distinct actions already tried + their outcomes + the
              current stall count + the live error fingerprint. Print THIS at the top of each turn
              so the loop never retries a known dead-end.
  status      Compact tail of the journal (last N records).
  selftest    Prove the fingerprint + stall logic deterministically — no files.

Usage:
    python3 scripts/loop_journal.py record --iteration 3 --action "add retry to fetch" \\
        --hypothesis "timeout is transient" --gate fail --gate-output test.log
    python3 scripts/loop_journal.py stall  [--k 3] [--exit-code]
    python3 scripts/loop_journal.py resume
    python3 scripts/loop_journal.py status [--n 10]
    python3 scripts/loop_journal.py selftest
"""
import hashlib
import json
import os
import re
import sys
import time

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
JOURNAL = os.path.join(LOOP_DIR, "journal.jsonl")
DEFAULT_K = 3

# Lines that carry the actual failure signal — we fingerprint THESE, not the whole log.
SIGNAL_RE = re.compile(
    r"(error|fail|failed|assert|assertion|exception|traceback|panic|fatal|"
    r"undefined|not found|cannot|unexpected|✗|✘|×)", re.I)

# Volatile tokens that differ run-to-run for the SAME bug — normalized away so the hash is stable.
_NORMALIZERS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),                      # pointers/addresses
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "HEX"),                     # sha/uuid-ish
    (re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}\S*", re.I), "TS"),  # ISO timestamps
    (re.compile(r"(:|line )\s*\d+(:\d+)?"), r"\1N"),                # file:line:col / "line 42"
    (re.compile(r"[/\\][\w./\\-]+/(\w+\.\w+)"), r"PATH/\1"),        # dir paths, keep basename
    (re.compile(r"0\.\d+s|\d+(\.\d+)?\s*(ms|s|sec|seconds)", re.I), "DUR"),  # durations
    (re.compile(r"\b\d+\b"), "N"),                                  # any remaining bare integer
    (re.compile(r"\s+"), " "),                                      # collapse whitespace
]


def log(msg):
    print("  " + msg)


def _read_source(spec):
    if spec is None:
        return ""
    if spec == "-" or spec is True:
        return sys.stdin.read()
    try:
        with open(spec, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def fingerprint(text):
    """Stable, model-free hash of a failure's SIGNATURE. Empty text -> '' (no failure)."""
    if not text or not text.strip():
        return ""
    signal = [ln.strip() for ln in text.splitlines() if SIGNAL_RE.search(ln)]
    # fall back to the last few non-empty lines if nothing matched the signal regex
    if not signal:
        signal = [ln.strip() for ln in text.splitlines() if ln.strip()][-5:]
    blob = "\n".join(signal[:20]).lower()
    for rx, repl in _NORMALIZERS:
        blob = rx.sub(repl, blob)
    return hashlib.sha1(blob.strip().encode("utf-8")).hexdigest()[:12]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load():
    rows = []
    if not os.path.exists(JOURNAL):
        return rows
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # one corrupt record must not lose the journal
    return rows


def cmd_record(opts):
    os.makedirs(LOOP_DIR, exist_ok=True)
    gate = opts.get("gate", "fail")
    fp = ""
    if gate != "pass":
        fp = fingerprint(_read_source(opts.get("gate-output")))
    rec = {
        "iteration": int(opts.get("iteration", 0)),
        "action": opts.get("action", ""),
        "hypothesis": opts.get("hypothesis", ""),
        "gate": gate,
        "fingerprint": fp,
        "note": opts.get("note", ""),
        "ts": opts.get("_now") or _now(),
    }
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log("recorded iter=%d gate=%s fp=%s action=%r" % (
        rec["iteration"], rec["gate"], rec["fingerprint"] or "-", rec["action"][:50]))
    print("recorded")


def cmd_fingerprint(opts):
    src = opts.get("file") or opts.get("input") or "-"
    print(fingerprint(_read_source(src)) or "(no-failure)")


def analyze(rows, k=DEFAULT_K):
    """Pure: journal rows -> stall verdict. Deterministic, no I/O.

    STALLED  = the last `k` attempts all failed with the SAME non-empty fingerprint.
    Also surfaces oscillation: actions tried >1× under that same fingerprint (the dead-ends).
    """
    if not rows:
        return {"verdict": "PROGRESS", "stall_count": 0, "fingerprint": "",
                "recommend": "continue", "dead_ends": [], "reason": "empty journal"}

    last = rows[-1]
    fp = last.get("fingerprint", "")
    if last.get("gate") == "pass" or not fp:
        return {"verdict": "PROGRESS", "stall_count": 0, "fingerprint": fp,
                "recommend": "continue", "dead_ends": [],
                "reason": "last attempt passed or had no failure signature"}

    # count the trailing run of consecutive failures sharing THIS fingerprint
    streak = 0
    for r in reversed(rows):
        if r.get("gate") != "pass" and r.get("fingerprint") == fp:
            streak += 1
        else:
            break

    # dead-end actions: actions that appear >1× under this exact fingerprint
    seen, dups = {}, []
    for r in rows:
        if r.get("fingerprint") == fp and r.get("gate") != "pass":
            a = (r.get("action") or "").strip()
            if not a:
                continue
            seen[a] = seen.get(a, 0) + 1
    dups = sorted([a for a, n in seen.items() if n > 1])

    if streak >= k:
        recommend = "escalate" if streak >= k + 1 else "switch-strategy"
        return {"verdict": "STALLED", "stall_count": streak, "fingerprint": fp,
                "recommend": recommend, "dead_ends": dups,
                "reason": "%d consecutive failures with the same fingerprint %s" % (streak, fp)}
    return {"verdict": "PROGRESS", "stall_count": streak, "fingerprint": fp,
            "recommend": "continue", "dead_ends": dups,
            "reason": "failing, but under the stall threshold (%d/%d)" % (streak, k)}


def cmd_stall(opts):
    k = int(opts.get("k", DEFAULT_K))
    a = analyze(_load(), k)
    if opts.get("json"):
        print(json.dumps(a, indent=2))
    else:
        print(a["verdict"].lower())
        log(a["reason"])
        if a["verdict"] == "STALLED":
            log("recommend: %s — do NOT re-feed the same goal into the same failure" % a["recommend"])
            if a["dead_ends"]:
                log("dead-end actions (already tried, same failure): %s" % "; ".join(a["dead_ends"]))
    if opts.get("exit-code") and a["verdict"] == "STALLED":
        sys.exit(10)


def cmd_resume(opts):
    """The read every turn should START with — what was tried, so we never repeat a dead-end."""
    rows = _load()
    if not rows:
        print("resume: fresh loop — no prior attempts")
        return
    a = analyze(rows, int(opts.get("k", DEFAULT_K)))
    passed = [r for r in rows if r.get("gate") == "pass"]
    print("resume: %d attempts · last gate=%s · stall=%s/%s · live_fp=%s" % (
        len(rows), rows[-1].get("gate"), a["stall_count"], opts.get("k", DEFAULT_K),
        a["fingerprint"] or "-"))
    log("verdict: %s — recommend: %s" % (a["verdict"], a["recommend"]))
    # distinct actions tried + their last outcome (anti-oscillation memory)
    last_outcome = {}
    for r in rows:
        act = (r.get("action") or "").strip()
        if act:
            last_outcome[act] = r.get("gate")
    for act, gate in list(last_outcome.items())[-12:]:
        log("tried [%s] %s" % (gate, act[:70]))
    if a["dead_ends"]:
        log("AVOID (dead-ends): %s" % "; ".join(a["dead_ends"]))
    if passed:
        log("resolved fingerprints so far: %d" % len({r.get("fingerprint") for r in passed}))


def cmd_status(opts):
    rows = _load()
    n = int(opts.get("n", 10))
    if not rows:
        print("journal empty")
        return
    print("journal: %d records (last %d):" % (len(rows), min(n, len(rows))))
    for r in rows[-n:]:
        log("iter=%-3s %-7s fp=%-12s %s" % (
            r.get("iteration"), r.get("gate"), r.get("fingerprint") or "-",
            (r.get("action") or "")[:56]))


def cmd_selftest(_opts):
    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-30s got=%s want=%s" % ("ok" if ok else "XX", name, got, want))

    # fingerprint stability: same bug with different line numbers / addresses / timestamps -> same hash
    a = fingerprint("FAILED test_login at app/auth.py:42  (0x7ffd, 2026-06-24T10:00:00Z) 1.3s")
    b = fingerprint("FAILED test_login at app/auth.py:99  (0x1abc, 2026-06-25T11:22:33Z) 0.4s")
    chk("fingerprint.stable", a == b and a != "", True)
    # a DIFFERENT failure -> different hash
    c = fingerprint("AssertionError: expected 3 got 4 in test_math")
    chk("fingerprint.distinct", c != a, True)
    chk("fingerprint.empty", fingerprint(""), "")

    base = {"hypothesis": "", "note": "", "ts": "t"}
    # 3 identical failures -> STALLED at k=3
    rows = [dict(base, iteration=i, action="retry fetch", gate="fail", fingerprint="deadbeef0001")
            for i in (1, 2, 3)]
    v = analyze(rows, 3)
    chk("stall.detected", v["verdict"], "STALLED")
    chk("stall.count", v["stall_count"], 3)
    chk("stall.deadend", v["dead_ends"], ["retry fetch"])
    # a pass on the latest turn -> PROGRESS, streak resets
    rows2 = rows + [dict(base, iteration=4, action="fix root cause", gate="pass", fingerprint="")]
    chk("progress.after_pass", analyze(rows2, 3)["verdict"], "PROGRESS")
    # two fails, different fingerprints -> not stalled (it's moving)
    rows3 = [dict(base, iteration=1, action="a", gate="fail", fingerprint="aaa1"),
             dict(base, iteration=2, action="b", gate="fail", fingerprint="bbb2")]
    chk("progress.moving", analyze(rows3, 3)["verdict"], "PROGRESS")
    # below threshold -> PROGRESS but streak counted
    chk("progress.under_k", analyze(rows[:2], 3)["stall_count"], 2)

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


def _parse(args):
    opts = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[key] = args[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    sub, opts = argv[0], _parse(argv[1:])
    {"record": cmd_record, "fingerprint": cmd_fingerprint, "stall": cmd_stall,
     "resume": cmd_resume, "status": cmd_status, "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: record fingerprint stall resume "
                               "status selftest" % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()
