#!/usr/bin/env python3
"""simplicio-loop — task anchor + drift guard (the loop's working memory for SCOPE).

`loop_journal.py` is the loop's memory of WHAT WAS TRIED (anti-oscillation). This is its sibling:
the loop's memory of WHAT THE TASK ACTUALLY IS (anti-DRIFT). A re-feed loop that remembers neither
can wander off the task ("desvio de tarefas") — it re-interprets the goal each turn, drops an
acceptance criterion, or declares "done" while items are still unaddressed. This worker freezes the
task's acceptance criteria at intake and makes three things deterministic and model-free:

  1. **Anchor** — freeze the goal + its acceptance criteria once, so every later turn re-reads the
     SAME contract instead of re-deriving it (and silently narrowing it).
  2. **Drift guard** — flag when the goal being worked this turn no longer matches the frozen goal,
     or when criteria remain unaddressed. The loop must re-anchor explicitly, never drift silently.
  3. **Done gate** — refuse to declare the task done / open a PR while ANY criterion is still
     pending. This is the evidence-gate for SCOPE: "done" requires every AC verified, with a
     receipt (file:line / command output / screenshot path) recorded per criterion.

It also renders the **item-by-item checklist** that `pr_evidence.py` drops into the PR body and the
source-item comment — so the PR shows a line per acceptance criterion with its status + evidence.

Deterministic and model-free: the fingerprint + coverage + drift math never call an LLM, so a resume
is reproducible from the on-disk anchor (same discipline as `loop_journal.py`).

State: `.orchestrator/loop/anchor.json` (override with $SIMPLICIO_ANCHOR_FILE):
    {"item", "goal", "goal_fp", "frozen_at",
     "criteria": [{"id","text","verify","status":"pending|partial|done","evidence","verified_at"}]}

Verbs:
  set        Freeze the goal + criteria. Criteria from --ac "text" (repeatable), --ac-file FILE
             (one per line; markdown `- [ ]`/`- [x]` lists understood), or stdin. RE-SET is
             idempotent: same goal → existing per-AC status/evidence are PRESERVED (progress is not
             reset). A CHANGED goal is refused unless --force (a silent goal swap IS drift).
             Inline verification can be declared as `--ac "text :: verify: <command or artifact>"`.
             Default lint rejects vague ACs like "works"; `--lint` also rejects short ACs (<3 words)
             unless a `verify:` method is declared.
  mark       Record progress on one criterion: --id ACk --status done|partial [--evidence "..."].
  status     Print the criteria table + coverage summary (e.g. "3/5 verified").
  checklist  Emit the markdown item-by-item checklist (for the PR body / evidence comment).
  check      Drift verdict for THIS turn: pass --goal "<goal worked now>"; ANCHORED (all verified) |
             INCOMPLETE (criteria pending) | DRIFT (goal changed / no anchor). --exit-code → 11 on DRIFT.
             --format text (default) | json | toon — `toon` renders the SAME verdict payload as
             `--json` in TOON (Token-Oriented Object Notation, github.com/toon-format/toon) instead
             of JSON, for the per-turn re-feed into the LLM's prompt (this is the "check every turn"
             call — quality-safety-delivery.md Step 4a). The on-disk anchor.json itself is unaffected
             — only this prompt-facing render can switch encoding.
  gate       The done/PR-open gate: READY only when every criterion is verified; else BLOCKED with
             the pending list. --exit-code → 12 when BLOCKED. Closing/opening a PR must pass this.
  selftest   Prove freeze/preserve/drift/coverage/gate/checklist deterministically — no files.

Usage:
    python3 scripts/task_anchor.py set --item 12 --goal "Add SSO login" \\
        --ac "Login page renders an SSO button" --ac "Clicking it redirects to the IdP"
    python3 scripts/task_anchor.py mark --id AC1 --status done --evidence "web_verify .orchestrator/tee/web/login.png"
    python3 scripts/task_anchor.py check --goal "Add SSO login" --exit-code
    python3 scripts/task_anchor.py check --goal "Add SSO login" --format toon
    python3 scripts/task_anchor.py gate --exit-code
    python3 scripts/task_anchor.py checklist
"""
import hashlib
import json
import os
import re
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
ANCHOR = os.environ.get("SIMPLICIO_ANCHOR_FILE") or os.path.join(LOOP_DIR, "anchor.json")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
from toon_codec import encode_toon  # noqa: E402 — prompt-facing render only, never the on-disk format

STATUSES = ("pending", "partial", "done")
_MD_CHECK = re.compile(r"^\s*[-*]\s*\[(?P<box>[ xX])\]\s*(?P<text>.+?)\s*$")
_MD_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")
_WS = re.compile(r"\s+")
VERIFY_RE = re.compile(r"^(?P<text>.*?)(?:\s*::\s*verify:\s*(?P<verify>.+))?$", re.I)
WORD_RE = re.compile(r"[A-Za-z0-9À-ÿ]+")
VAGUE_AC_RES = [
    re.compile(r"^(?:it\s+)?works$", re.I),
    re.compile(r"^properly$", re.I),
    re.compile(r"^ok(?:ay)?$", re.I),
    re.compile(r"^done$", re.I),
    re.compile(r"^(?:tudo\s+)?funciona(?:\s+corretamente)?$", re.I),
    re.compile(r"^est[aá]\s+(?:bom|ok)$", re.I),
]


def log(msg):
    print("  " + msg)


def _emit_progress(step, status, **kw):
    """Fail-open progress-feedback hook (#299) — never raises, never blocks the anchor worker."""
    try:
        import loop_progress
        loop_progress.emit_event(step, status=status, source="task_anchor.py", **kw)
    except Exception:
        pass


def _now():
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----- pure helpers (selftest exercises these directly, no I/O) -----------------------------------

def goal_fingerprint(goal):
    """Stable, model-free hash of a goal's normalized text. Empty -> ''."""
    if not goal or not goal.strip():
        return ""
    norm = _WS.sub(" ", goal.strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def split_verify(raw):
    """Split `text :: verify: ...` into (text, verify). Missing suffix -> ('text', '')."""
    m = VERIFY_RE.match((raw or "").strip())
    text = (m.group("text") if m else (raw or "")).strip()
    verify = (m.group("verify") if m and m.group("verify") else "").strip()
    return text, verify


def parse_criteria(lines):
    """Turn raw lines (plain, or markdown checklist/bullets) into AC texts, in order, deduped."""
    out, seen = [], set()
    for raw in lines:
        if raw is None:
            continue
        m = _MD_CHECK.match(raw) or _MD_BULLET.match(raw)
        text = (m.group("text") if m else raw).strip()
        if not text:
            continue
        bare, _verify = split_verify(text)
        if not bare:
            continue
        key = _WS.sub(" ", bare.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def lint_criteria(texts, strict=False):
    """Return a list of lint errors for AC text. Default rejects vague ACs; strict also rejects
    short ACs (<3 words) unless a verify method is declared."""
    failures = []
    for raw in texts or []:
        text, verify = split_verify(raw)
        norm = text.strip().rstrip(".!?").strip()
        if not norm:
            continue
        if any(rx.match(norm) for rx in VAGUE_AC_RES):
            failures.append("vague acceptance criterion refused: %r" % text)
            continue
        words = WORD_RE.findall(norm)
        if strict and len(words) < 3 and not verify:
            failures.append("strict lint refused short acceptance criterion without verify: %r" %
                            text)
    return failures


def freeze_criteria(texts):
    """Build the criteria list with stable AC ids and a pending status each."""
    out = []
    for i, raw in enumerate(texts):
        text, verify = split_verify(raw)
        out.append({"id": "AC%d" % (i + 1), "text": text, "verify": verify, "status": "pending",
                    "evidence": "", "verified_at": ""})
    return out


def merge_preserving(old, new_texts):
    """Re-freeze to new_texts but PRESERVE status/evidence for criteria whose text is unchanged.

    Progress is keyed by normalized text, not position, so reordering/adding ACs keeps prior work.
    """
    by_text = {_WS.sub(" ", c.get("text", "").lower()): c for c in (old or [])}
    merged = []
    for i, raw in enumerate(new_texts):
        text, verify = split_verify(raw)
        prev = by_text.get(_WS.sub(" ", text.lower()))
        if prev:
            merged.append({"id": "AC%d" % (i + 1), "text": text,
                           "verify": verify or prev.get("verify", ""),
                           "status": prev.get("status", "pending"),
                           "evidence": prev.get("evidence", ""),
                           "verified_at": prev.get("verified_at", "")})
        else:
            merged.append({"id": "AC%d" % (i + 1), "text": text, "verify": verify,
                           "status": "pending",
                           "evidence": "", "verified_at": ""})
    return merged


def coverage(criteria):
    """(done, total, pending_ids). 'done' counts only fully-verified criteria."""
    total = len(criteria)
    done = sum(1 for c in criteria if c.get("status") == "done")
    pending = [c.get("id") for c in criteria if c.get("status") != "done"]
    return done, total, pending


def drift_verdict(anchor, goal_now):
    """Pure: anchor + the goal being worked now -> verdict dict."""
    if not anchor or not anchor.get("goal_fp"):
        return {"verdict": "DRIFT", "reason": "no task anchor set — freeze the ACs first (set)",
                "pending": [], "coverage": "0/0"}
    fp_now = goal_fingerprint(goal_now) if goal_now is not None else anchor["goal_fp"]
    if goal_now is not None and fp_now != anchor["goal_fp"]:
        return {"verdict": "DRIFT",
                "reason": "the goal worked this turn != the frozen goal (re-anchor with --force "
                          "if the task genuinely changed)",
                "pending": [c.get("id") for c in anchor.get("criteria", [])
                            if c.get("status") != "done"],
                "coverage": "%d/%d" % coverage(anchor.get("criteria", []))[:2]}
    done, total, pending = coverage(anchor.get("criteria", []))
    if total and not pending:
        return {"verdict": "ANCHORED", "reason": "every acceptance criterion verified",
                "pending": [], "coverage": "%d/%d" % (done, total)}
    return {"verdict": "INCOMPLETE",
            "reason": "%d/%d criteria verified — %d still open" % (done, total, len(pending)),
            "pending": pending, "coverage": "%d/%d" % (done, total)}


def render_checklist(criteria, heading="Acceptance criteria (item-by-item)"):
    """Markdown item-by-item checklist with per-AC status + evidence."""
    mark = {"done": "x", "partial": "~", "pending": " "}
    lines = ["### %s" % heading] if heading else []
    if not criteria:
        lines.append("- _(no acceptance criteria were anchored for this item)_")
        return "\n".join(lines)
    for c in criteria:
        box = mark.get(c.get("status"), " ")
        line = "- [%s] **%s** %s" % (box, c.get("id"), c.get("text"))
        verify = (c.get("verify") or "").strip()
        if verify:
            line += " — _verify:_ %s" % verify
        ev = (c.get("evidence") or "").strip()
        if ev:
            line += " — _evidence:_ %s" % ev
        elif c.get("status") != "done":
            line += " — _pending_"
        lines.append(line)
    done, total, _ = coverage(criteria)
    lines.append("")
    lines.append("**Coverage:** %d/%d criteria verified." % (done, total))
    return "\n".join(lines)


# ----- I/O + commands ----------------------------------------------------------------------------

def _load():
    if not os.path.exists(ANCHOR):
        return {}
    try:
        with open(ANCHOR, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(anchor):
    os.makedirs(os.path.dirname(ANCHOR), exist_ok=True)
    with open(ANCHOR, "w", encoding="utf-8") as f:
        json.dump(anchor, f, ensure_ascii=False, indent=2)


def _collect_ac(opts):
    lines = []
    ac = opts.get("ac")
    if isinstance(ac, list):
        lines += ac
    elif isinstance(ac, str):
        lines.append(ac)
    f = opts.get("ac-file")
    if isinstance(f, str) and os.path.exists(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            lines += fh.read().splitlines()
    if opts.get("stdin") or (not lines and not sys.stdin.isatty()):
        try:
            lines += sys.stdin.read().splitlines()
        except Exception:
            pass
    return parse_criteria(lines)


def cmd_set(opts):
    goal = opts.get("goal") or ""
    if not goal.strip():
        print("anchor: refusing to freeze — --goal is required")
        sys.exit(2)
    texts = _collect_ac(opts)
    if not texts:
        print("anchor: refusing to freeze — no acceptance criteria given "
              "(--ac / --ac-file / stdin). An item with no AC is itself a drift risk.")
        sys.exit(2)
    lint_errors = lint_criteria(texts, strict=bool(opts.get("lint")))
    if lint_errors:
        for msg in lint_errors:
            print("anchor: %s" % msg)
        sys.exit(2)
    fp = goal_fingerprint(goal)
    existing = _load()
    if existing and existing.get("goal_fp") and existing["goal_fp"] != fp and not opts.get("force"):
        print("anchor: BLOCKED — a different goal is already anchored (goal changed). "
              "This is exactly the drift signal. Re-anchor with --force only if the task "
              "genuinely changed.")
        sys.exit(12)
    criteria = (merge_preserving(existing.get("criteria"), texts)
                if existing.get("goal_fp") == fp else freeze_criteria(texts))
    anchor = {"item": opts.get("item") or existing.get("item", ""), "goal": goal, "goal_fp": fp,
              "frozen_at": existing.get("frozen_at") or _now(), "criteria": criteria}
    _save(anchor)
    done, total, _ = coverage(criteria)
    log("anchored item=%s · %d criteria (%d already verified) · fp=%s" % (
        anchor["item"] or "-", total, done, fp))
    print("anchored")
    _emit_progress("triage", "end", item=anchor.get("item") or None,
                   detail="anchor congelado: %d ACs" % total)


def cmd_mark(opts):
    anchor = _load()
    if not anchor.get("criteria"):
        print("anchor: no anchor set — run `set` first")
        sys.exit(2)
    cid = (opts.get("id") or "").strip()
    status = (opts.get("status") or "").strip().lower()
    if status not in STATUSES:
        print("anchor: --status must be one of %s" % ", ".join(STATUSES))
        sys.exit(2)
    hit = None
    for c in anchor["criteria"]:
        if c.get("id") == cid:
            hit = c
            break
    if not hit:
        print("anchor: no criterion %r (have %s)" % (
            cid, ", ".join(c.get("id") for c in anchor["criteria"])))
        sys.exit(2)
    if status == "done" and not (opts.get("evidence") or "").strip():
        print("anchor: BLOCKED — marking %s done requires --evidence "
              "(file:line / command output / screenshot path). No receipt, no done." % cid)
        sys.exit(12)
    hit["status"] = status
    hit["evidence"] = (opts.get("evidence") or hit.get("evidence") or "").strip()
    hit["verified_at"] = _now() if status == "done" else ""
    _save(anchor)
    done, total, _ = coverage(anchor["criteria"])
    log("%s -> %s (%d/%d verified)" % (cid, status, done, total))
    print("marked")
    if status == "done":
        _emit_progress("journal", "end", item=anchor.get("item") or None, outcome="pass",
                       detail="AC %s verificado (%d/%d)" % (cid, done, total))


def cmd_status(opts):
    anchor = _load()
    as_json = bool(opts.get("json"))
    if not anchor.get("criteria"):
        if as_json:
            print(json.dumps({"set": False, "item": None, "goal_fp": None, "frozen_at": None,
                              "criteria": [], "done": 0, "total": 0, "pending": []},
                             ensure_ascii=False))
        else:
            print("anchor: none set")
        return
    done, total, pending = coverage(anchor["criteria"])
    if as_json:
        print(json.dumps({
            "set": True,
            "item": anchor.get("item") or None,
            "goal_fp": anchor.get("goal_fp"),
            "frozen_at": anchor.get("frozen_at"),
            "criteria": anchor["criteria"],
            "done": done, "total": total, "pending": pending,
        }, ensure_ascii=False))
        return
    print("anchor: item=%s · goal_fp=%s · frozen=%s" % (
        anchor.get("item") or "-", anchor.get("goal_fp"), anchor.get("frozen_at")))
    for c in anchor["criteria"]:
        detail = c.get("text")
        if c.get("verify"):
            detail += " [verify: %s]" % c.get("verify")
        if c.get("evidence"):
            detail += "  <%s>" % c["evidence"]
        log("[%-7s] %-4s %s" % (c.get("status"), c.get("id"), detail))
    log("coverage: %d/%d verified%s" % (
        done, total, ("" if not pending else " · pending: " + ", ".join(pending))))


def cmd_checklist(opts):
    print(render_checklist(_load().get("criteria", [])))


def cmd_check(opts):
    anchor = _load()
    goal_now = opts.get("goal")
    v = drift_verdict(anchor, goal_now if isinstance(goal_now, str) else None)
    fmt = (opts.get("format") or ("json" if opts.get("json") else "text")).strip().lower()
    if fmt == "toon":
        # TOON (Token-Oriented Object Notation, github.com/toon-format/toon): same verdict payload
        # as --format json, rendered leaner for the per-turn prompt re-feed. The on-disk anchor.json
        # stays plain JSON — only this prompt-facing render switches encoding (#88).
        print(encode_toon(v))
    elif fmt == "json":
        print(json.dumps(v, indent=2, ensure_ascii=False))
    else:
        print(v["verdict"].lower())
        log(v["reason"])
        if v["pending"]:
            log("pending criteria: %s" % ", ".join(v["pending"]))
    if v["verdict"] == "DRIFT":
        _emit_progress("triage", "blocked", outcome="blocked",
                       detail="DRIFT detectado — re-anchor necessário: %s" % v["reason"])
    if opts.get("exit-code") and v["verdict"] == "DRIFT":
        sys.exit(11)


def cmd_gate(opts):
    anchor = _load()
    criteria = anchor.get("criteria", [])
    done, total, pending = coverage(criteria)
    ready = bool(total) and not pending
    if ready:
        print("ready")
        log("all %d acceptance criteria verified — safe to declare done / open the PR" % total)
    else:
        print("blocked")
        if not total:
            log("no anchor set — freeze the acceptance criteria before declaring done")
        else:
            log("%d/%d verified — do NOT declare done or open the PR yet" % (done, total))
            log("pending: %s" % ", ".join(pending))
    if opts.get("exit-code") and not ready:
        sys.exit(12)


def cmd_selftest(_opts):
    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-32s got=%r want=%r" % ("ok" if ok else "XX", name, got, want))

    # goal fingerprint: whitespace/case-insensitive, stable; different goal -> different hash
    chk("fp.stable", goal_fingerprint("Add SSO  login") == goal_fingerprint("add sso login"), True)
    chk("fp.distinct", goal_fingerprint("a") != goal_fingerprint("b"), True)
    chk("fp.empty", goal_fingerprint(""), "")

    # parse: plain + markdown checklist + bullets, deduped in order
    texts = parse_criteria(["Renders a button", "- [ ] Redirects to IdP",
                            "- [x] Logs the user in", "* plain bullet", "Renders a button"])
    chk("parse.count", len(texts), 4)
    chk("parse.strip_md", texts[1], "Redirects to IdP")

    crit = freeze_criteria(texts)
    chk("freeze.ids", [c["id"] for c in crit], ["AC1", "AC2", "AC3", "AC4"])
    chk("freeze.pending", all(c["status"] == "pending" for c in crit), True)
    chk("split.basic", split_verify("Works :: verify: python3 scripts/check.py"),
        ("Works", "python3 scripts/check.py"))
    chk("split.none", split_verify("One AC"), ("One AC", ""))

    # coverage + gate logic
    crit[0]["status"] = "done"
    crit[0]["evidence"] = "test.py:10"
    chk("coverage.partial", coverage(crit)[:2], (1, 4))
    chk("drift.incomplete", drift_verdict({"goal_fp": "x", "criteria": crit}, None)["verdict"],
        "INCOMPLETE")
    for c in crit:
        c["status"] = "done"
    chk("drift.anchored", drift_verdict({"goal_fp": "x", "criteria": crit}, None)["verdict"],
        "ANCHORED")

    # drift: a changed goal this turn is flagged DRIFT
    anc = {"goal_fp": goal_fingerprint("original task"), "criteria": crit}
    chk("drift.goal_changed", drift_verdict(anc, "a totally different task")["verdict"], "DRIFT")
    chk("drift.same_goal", drift_verdict(anc, "original task")["verdict"], "ANCHORED")
    chk("drift.no_anchor", drift_verdict({}, "x")["verdict"], "DRIFT")

    # merge preserves progress across a re-set that adds an AC
    old = [{"id": "AC1", "text": "Renders a button", "status": "done", "evidence": "e",
            "verified_at": "t", "verify": ""}]
    merged = merge_preserving(old, ["Renders a button :: verify: shot.png", "A new criterion"])
    chk("merge.preserve", merged[0]["status"], "done")
    chk("merge.new_pending", merged[1]["status"], "pending")
    chk("merge.verify_preserved", merged[0]["verify"], "shot.png")
    chk("freeze.verify_carried", freeze_criteria(["A :: verify: cmd"])[0]["verify"], "cmd")

    # checklist renders boxes + coverage
    cl = render_checklist(crit)
    chk("checklist.box", "[x]" in cl, True)
    chk("checklist.coverage", "Coverage:" in cl, True)
    chk("lint.vague_refused", bool(lint_criteria(["works"])), True)
    chk("lint.default_allows_short", lint_criteria(["a1"]), [])
    chk("lint.strict_refuses_short", bool(lint_criteria(["a1"], strict=True)), True)
    chk("lint.strict_allows_short_with_verify", lint_criteria(["a1 :: verify: shot.png"],
                                                              strict=True), [])

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


def _parse(args):
    """Parse --k v / --flag, collecting repeated --ac into a list."""
    opts = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                val = args[i + 1]
                if key in opts:
                    if not isinstance(opts[key], list):
                        opts[key] = [opts[key]]
                    opts[key].append(val)
                else:
                    opts[key] = val
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
    # --describe-cli: emit JSON spec of accepted verbs + flags
    if argv[0] == "--describe-cli":
        import json
        print(json.dumps({
            "verbs": ["set", "mark", "status", "checklist", "check", "gate", "selftest"],
            "flags": ["--item", "--goal", "--ac", "--ac-file", "--force", "--id", "--status",
                      "--evidence", "--format", "--exit-code", "--lint", "--require-evidence",
                      "--out", "--json", "--help"],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    {"set": cmd_set, "mark": cmd_mark, "status": cmd_status, "checklist": cmd_checklist,
     "check": cmd_check, "gate": cmd_gate, "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: set mark status checklist check "
                               "gate selftest" % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()
