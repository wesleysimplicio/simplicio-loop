#!/usr/bin/env python3
"""simplicio-loop — task backlog + genesis guard (the loop's working memory for the DECOMPOSITION).

`loop_journal.py` is the loop's memory of WHAT WAS TRIED; `task_anchor.py` is its memory of WHAT
THE TASK IS. This is the third sibling: the memory of WHAT THE TASKS ARE — the multi-item
decomposition of a vague goal, frozen ABOVE the per-item anchor. Zero AC/plan generation happens
here: the LLM does the brainstorm (subtasks, per-item acceptance criteria, dependencies); this
worker FREEZES, ORDERS and GATES it — "the AI decides, workers act", enforced not just described:

  1. **Freeze** — `init` refuses an empty decomposition, an item with zero ACs, an unknown or
     cyclic `depends_on`; a changed master goal needs `--force`. Re-`init` with the same goal is
     idempotent (per-item state/evidence preserved by normalized item-goal text).
  2. **Genesis guard** — `genesis` deterministically detects an empty/greenfield repo (zero code
     files under `--root`); on such a repo `init` REFUSES without `--genesis`, and with it demands
     exactly one item tagged `scaffold`, reorders it to T1, and makes every other item depend on it.
  3. **Drain + done gate** — `next` claims one item at a time (honoring `depends_on`) and prints
     the ready-to-run `task_anchor.py set` arming command; `done` refuses (exit 12) unless the
     ARMED anchor is THIS item (same goal fingerprint) with every AC verified. An LLM that forgot
     to arm/verify the anchor cannot close a backlog item.

Deterministic and model-free: fingerprints reuse `task_anchor.goal_fingerprint` byte-for-byte, so
the `done` <-> anchor coupling is exact; the genesis classifier is a fixed extension set, no LLM.

State: `.orchestrator/backlog/backlog.jsonl` (override with $SIMPLICIO_BACKLOG_FILE), one JSON
record per line, rewritten atomically:
    {"type":"master","goal","goal_fp","frozen_at","genesis"}
    {"type":"item","id":"T1","goal","goal_fp","acs":[...],"state":"open|claimed|done|skipped",
     "depends_on":[],"tags":[],"provenance":"llm-decomposition","claimed_at","done_at",
     "evidence","reason"}

Verbs:
  init      Freeze the LLM's decomposition: --goal "<master goal>" + a JSON array of items via
            --items-file FILE or stdin (--stdin). Each item: {"goal","acs":[...],
            "depends_on":[],"tags":[]} (optional "id" for in-file depends_on references; final
            ids are always reassigned T1..Tn). --genesis for a greenfield repo; --root DIR for
            the genesis detector (default: cwd); --force to replace a different frozen goal.
  genesis   Standalone greenfield detector for --root (default: cwd). Prints genesis|code;
            --exit-code -> exit 10 when genesis.
  next      Claim the next workable item: re-prints an already-claimed item (one in flight),
            else claims the first open item whose depends_on are all done. Prints the anchor
            arming command. All items done|skipped -> prints exactly `empty` (the drain dry
            signal). --json / --format toon.
  done      Close one item: --id Tk. Exit 12 unless the anchor file exists, its goal_fp equals
            the item's (the armed anchor IS this item), and no anchor AC is pending. On pass the
            per-AC evidence summary is copied onto the item.
  skip      Quarantine one item: --id Tk --reason "..." (reason required). A skipped item never
            blocks the dry condition.
  status    Master fingerprint + per-state counts + one line per item.
  check     Backlog integrity/drift verdict: BACKLOG_OK | DRIFT (no backlog / --goal fingerprint
            diverges / corrupt line / unknown dep). --exit-code -> 11 on DRIFT. --json.
  selftest  Prove freeze/order/genesis/done-gate/merge/dry deterministically — no files.

Usage:
    python3 scripts/task_backlog.py genesis --root . --exit-code
    python3 scripts/task_backlog.py init --goal "Build the API" --items-file plan.json
    python3 scripts/task_backlog.py next
    python3 scripts/task_backlog.py done --id T1
    python3 scripts/task_backlog.py check --goal "Build the API" --exit-code
"""
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
BACKLOG_DIR = os.path.join(REPO, ".orchestrator", "backlog")
BACKLOG = os.environ.get("SIMPLICIO_BACKLOG_FILE") or os.path.join(BACKLOG_DIR, "backlog.jsonl")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
# Same fingerprint/coverage/merge math as the anchor — the `done` <-> anchor coupling is exact.
from task_anchor import ANCHOR, coverage, goal_fingerprint, merge_preserving  # noqa: E402

STATES = ("open", "claimed", "done", "skipped")
SCAFFOLD_TAG = "scaffold"
_WS = re.compile(r"\s+")

# genesis classifier: a repo is genesis iff it has ZERO files with a code extension (fixed set),
# after excluding dot-dirs/dotfiles, `.orchestrator`/`.simplicio`, README*/LICENSE* and `*.md`.
CODE_EXTS = frozenset((
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cc", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
    ".ps1", ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".pl", ".r", ".ex",
    ".exs", ".erl", ".hs", ".ml", ".zig", ".dart", ".m", ".mm",
))


def log(msg):
    print("  " + msg)


def _now():
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----- pure helpers (selftest exercises these directly, no I/O) -----------------------------------

def _norm(text):
    return _WS.sub(" ", (text or "").strip().lower())


def classify_genesis(rel_paths):
    """Pure: relative file paths -> True when the repo is genesis (no code file survives the
    exclusions). Dotfiles/dot-dirs, `.orchestrator`/`.simplicio`, README*/LICENSE* and `*.md`
    never count as code."""
    for rel in rel_paths:
        parts = rel.replace("\\", "/").split("/")
        if any(p.startswith(".") for p in parts[:-1]):
            continue  # dot-dir (covers .git/.orchestrator/.simplicio/...)
        name = parts[-1]
        if name.startswith("."):
            continue  # dotfile
        upper = name.upper()
        if upper.startswith("README") or upper.startswith("LICENSE"):
            continue
        if name.lower().endswith(".md"):
            continue
        if os.path.splitext(name)[1].lower() in CODE_EXTS:
            return False
    return True


def validate_items(raw_items):
    """Pure: the LLM's decomposition -> error string, or None when freezable. Fail-closed:
    no items, an item with no goal / zero ACs, a duplicate id, an unknown dep, or a cycle all
    refuse — mirroring the anchor's "an item with no AC is itself a drift risk"."""
    if not isinstance(raw_items, list) or not raw_items:
        return "no items — a backlog with nothing in it is not a decomposition"
    ids = []
    for i, it in enumerate(raw_items):
        if not isinstance(it, dict) or not _norm(it.get("goal")):
            return "item %d has no goal text" % (i + 1)
        acs = [a for a in (it.get("acs") or []) if isinstance(a, str) and a.strip()]
        if not acs:
            return ("item %d (%r) has zero acceptance criteria — an item with no AC is itself "
                    "a drift risk" % (i + 1, it.get("goal")))
        ids.append(str(it.get("id") or "T%d" % (i + 1)))
    if len(set(ids)) != len(ids):
        return "duplicate item ids in the decomposition"
    deps = {}
    for i, it in enumerate(raw_items):
        for d in (it.get("depends_on") or []):
            if str(d) not in ids:
                return "item %s depends_on unknown item %r" % (ids[i], d)
        deps[ids[i]] = [str(d) for d in (it.get("depends_on") or [])]
    # mandatory topo-sort: a cycle means the decomposition can never drain
    seen, done = set(), set()

    def _cyclic(node):
        if node in done:
            return False
        if node in seen:
            return True
        seen.add(node)
        if any(_cyclic(d) for d in deps.get(node, [])):
            return True
        done.add(node)
        return False

    if any(_cyclic(n) for n in ids):
        return "depends_on cycle — the decomposition can never drain"
    return None


def order_for_genesis(raw_items):
    """Pure: reorder so the single `scaffold`-tagged item leads, and every other item depends on
    it -> (ordered_items, error). Exactly one scaffold item is required on a genesis repo.
    Ids are materialized (in ORIGINAL order) before reordering, so in-file depends_on references
    stay valid and `freeze_items` can remap them to the final T1..Tn."""
    items = []
    for i, it in enumerate(raw_items):
        it = dict(it)
        it["id"] = str(it.get("id") or "T%d" % (i + 1))
        items.append(it)
    scaffolds = [it for it in items
                 if SCAFFOLD_TAG in [str(t).lower() for t in (it.get("tags") or [])]]
    if len(scaffolds) != 1:
        return None, ("a genesis repo needs exactly 1 item tagged '%s' (got %d) — structure, "
                      "toolchain and one minimal green test come first" % (
                          SCAFFOLD_TAG, len(scaffolds)))
    scaffold = scaffolds[0]
    ordered = [scaffold] + [it for it in items if it is not scaffold]
    for it in ordered[1:]:  # every non-scaffold item depends on the scaffold
        dep = [str(d) for d in (it.get("depends_on") or [])]
        if scaffold["id"] not in dep:
            dep.append(scaffold["id"])
        it["depends_on"] = dep
    return ordered, None


def freeze_items(master_goal, raw_items, genesis):
    """Pure: validated (+ genesis-ordered) raw items -> (master, items) with final ids T1..Tn
    (depends_on remapped from in-file ids) and every item open."""
    old_ids = [str(it.get("id") or "T%d" % (i + 1)) for i, it in enumerate(raw_items)]
    remap = {old: "T%d" % (i + 1) for i, old in enumerate(old_ids)}
    items = []
    for i, it in enumerate(raw_items):
        goal = it["goal"].strip()
        items.append({"type": "item", "id": "T%d" % (i + 1), "goal": goal,
                      "goal_fp": goal_fingerprint(goal),
                      "acs": [a.strip() for a in it.get("acs") or [] if a.strip()],
                      "state": "open",
                      "depends_on": sorted({remap[str(d)] for d in (it.get("depends_on") or [])}),
                      "tags": [str(t).lower() for t in (it.get("tags") or [])],
                      "provenance": "llm-decomposition",
                      "claimed_at": "", "done_at": "", "evidence": "", "reason": ""})
    master = {"type": "master", "goal": master_goal, "goal_fp": goal_fingerprint(master_goal),
              "frozen_at": _now(), "genesis": bool(genesis)}
    return master, items


def merge_items(old_items, new_items):
    """Pure: re-freeze to new_items but PRESERVE per-item progress keyed by normalized goal text
    (same discipline as the anchor's merge_preserving, which does the state/evidence carry)."""
    carried = merge_preserving(
        [{"text": it["goal"], "status": it.get("state", "open"),
          "evidence": it.get("evidence", ""), "verified_at": it.get("done_at", "")}
         for it in (old_items or [])],
        [it["goal"] for it in new_items])
    by_text = {_norm(it["goal"]): it for it in (old_items or [])}
    merged = []
    for it, prev in zip(new_items, carried):
        it = dict(it)
        if prev["status"] != "pending":  # a text merge_preserving matched — carry the progress
            it["state"] = prev["status"] if prev["status"] in STATES else "open"
            it["evidence"] = prev["evidence"]
            it["done_at"] = prev["verified_at"]
            old = by_text.get(_norm(it["goal"])) or {}
            it["claimed_at"] = old.get("claimed_at", "")
            it["reason"] = old.get("reason", "")
        merged.append(it)
    return merged


def pick_next(items):
    """Pure: -> (kind, item). kind is 'claimed' (one already in flight — re-print it),
    'ready' (first open item with all depends_on done), 'empty' (everything done|skipped — the
    drain dry signal), or 'blocked' (open items remain but none workable, e.g. a dep was
    skipped — quarantine or re-plan, never silently drop)."""
    for it in items:
        if it.get("state") == "claimed":
            return "claimed", it
    done_ids = {it["id"] for it in items if it.get("state") == "done"}
    for it in items:
        if it.get("state") == "open" and all(d in done_ids for d in it.get("depends_on") or []):
            return "ready", it
    if all(it.get("state") in ("done", "skipped") for it in items):
        return "empty", None
    return "blocked", None


def done_verdict(item, anchor):
    """Pure: the item + the on-disk anchor -> error string, or None when the item may close.
    Refuses when no anchor is armed, the armed anchor is a DIFFERENT goal, or any AC is pending."""
    if not anchor or not anchor.get("goal_fp"):
        return "no anchor armed — run the `task_anchor.py set` command `next` printed first"
    if anchor["goal_fp"] != item.get("goal_fp"):
        return ("the armed anchor is a different goal (fp %s != item fp %s) — arm THIS item "
                "before closing it" % (anchor["goal_fp"], item.get("goal_fp")))
    done, total, pending = coverage(anchor.get("criteria", []))
    if not total or pending:
        return ("anchor coverage %d/%d — pending: %s. Every AC needs a verified receipt before "
                "the item closes" % (done, total, ", ".join(pending) or "-"))
    return None


def anchor_arm_command(item):
    """The ready-to-run arming command `next` prints. --force is legitimate here: switching the
    anchor to a freshly claimed item IS the deliberate re-anchor."""
    parts = ["python3 scripts/task_anchor.py set", "--item %s" % item["id"],
             '--goal "%s"' % item["goal"].replace('"', "'"), "--force"]
    parts += ['--ac "%s"' % a.replace('"', "'") for a in item.get("acs", [])]
    return " ".join(parts)


# ----- I/O + commands ----------------------------------------------------------------------------

def _walk_files(root):
    out = []
    for r, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            out.append(os.path.relpath(os.path.join(r, n), root))
    return out


def _load():
    """-> (master, items, error)."""
    if not os.path.exists(BACKLOG):
        return None, [], None
    master, items = None, []
    try:
        with open(BACKLOG, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") == "master":
                    master = rec
                elif rec.get("type") == "item":
                    items.append(rec)
    except (OSError, ValueError) as e:
        return None, [], "corrupt backlog line: %s" % e
    if master is None:
        return None, [], "backlog has no master record"
    return master, items, None


def _save(master, items):
    os.makedirs(os.path.dirname(BACKLOG) or ".", exist_ok=True)
    tmp = BACKLOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in [master] + items:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, BACKLOG)


def _load_anchor():
    if not os.path.exists(ANCHOR):
        return {}
    try:
        with open(ANCHOR, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _read_items(opts):
    raw = None
    f = opts.get("items-file")
    if isinstance(f, str):
        if not os.path.exists(f):
            print("backlog: --items-file %s not found" % f)
            sys.exit(2)
        with open(f, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    elif opts.get("stdin") or not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = None
    if raw is None or not raw.strip():
        print("backlog: refusing to freeze — no items given (--items-file / --stdin)")
        sys.exit(2)
    try:
        return json.loads(raw)
    except ValueError as e:
        print("backlog: items are not valid JSON: %s" % e)
        sys.exit(2)


def _is_genesis(opts):
    root = opts.get("root") if isinstance(opts.get("root"), str) else os.getcwd()
    return classify_genesis(_walk_files(root)), root


def cmd_init(opts):
    goal = opts.get("goal") or ""
    if not isinstance(goal, str) or not goal.strip():
        print("backlog: refusing to freeze — --goal is required")
        sys.exit(2)
    raw_items = _read_items(opts)
    err = validate_items(raw_items)
    if err:
        print("backlog: refusing to freeze — %s" % err)
        sys.exit(2)
    genesis, root = _is_genesis(opts)
    if genesis and not opts.get("genesis"):
        print("backlog: BLOCKED — %s is a GENESIS repo (no code yet). Re-run init --genesis "
              "with a first item tagged '%s' (structure + toolchain + one minimal green test)."
              % (root, SCAFFOLD_TAG))
        sys.exit(12)
    if opts.get("genesis"):
        raw_items, err = order_for_genesis(raw_items)
        if err:
            print("backlog: refusing to freeze — %s" % err)
            sys.exit(2)
    fp = goal_fingerprint(goal)
    existing_master, existing_items, _err = _load()
    if existing_master and existing_master.get("goal_fp") != fp and not opts.get("force"):
        print("backlog: BLOCKED — a different master goal is already frozen (goal changed). "
              "This is exactly the drift signal. Re-init with --force only if the body of work "
              "genuinely changed.")
        sys.exit(12)
    master, items = freeze_items(goal.strip(), raw_items, bool(opts.get("genesis")))
    if existing_master and existing_master.get("goal_fp") == fp:
        items = merge_items(existing_items, items)  # idempotent re-init preserves progress
        master["frozen_at"] = existing_master.get("frozen_at") or master["frozen_at"]
    _save(master, items)
    log("frozen %d items · fp=%s%s" % (len(items), fp,
                                       " · genesis (scaffold leads)" if master["genesis"] else ""))
    print("frozen")


def cmd_genesis(opts):
    genesis, root = _is_genesis(opts)
    print("genesis" if genesis else "code")
    log("root=%s · %s" % (root, "no code files found — greenfield" if genesis
                          else "code files present"))
    if opts.get("exit-code") and genesis:
        sys.exit(10)


def cmd_next(opts):
    master, items, err = _load()
    if err or master is None:
        print("backlog: none frozen — run `init` first%s" % ((" (%s)" % err) if err else ""))
        sys.exit(2)
    kind, it = pick_next(items)
    if kind == "empty":
        print("empty")
        return
    if kind == "blocked":
        print("blocked")
        log("open items remain but none is workable (a dependency was skipped?) — "
            "skip the dependents too, or re-init the decomposition")
        return
    if kind == "ready":
        it["state"] = "claimed"
        it["claimed_at"] = _now()
        _save(master, items)
    payload = {"verdict": kind, "id": it["id"], "goal": it["goal"], "goal_fp": it["goal_fp"],
               "acs": it["acs"], "depends_on": it["depends_on"], "tags": it["tags"],
               "arm": anchor_arm_command(it)}
    fmt = (opts.get("format") or ("json" if opts.get("json") else "text")).strip().lower()
    if fmt == "toon":
        from toon_codec import encode_toon  # prompt-facing render only, like the anchor's check
        print(encode_toon(payload))
    elif fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("%s %s" % ("re-printing claimed" if kind == "claimed" else "claimed", it["id"]))
        log(it["goal"])
        for a in it["acs"]:
            log("- %s" % a)
        log("arm the anchor before working it:")
        log(anchor_arm_command(it))


def _find(items, cid):
    for it in items:
        if it.get("id") == cid:
            return it
    return None


def cmd_done(opts):
    master, items, err = _load()
    if err or master is None:
        print("backlog: none frozen — run `init` first")
        sys.exit(2)
    it = _find(items, (opts.get("id") or "").strip())
    if not it:
        print("backlog: no item %r (have %s)" % (opts.get("id"),
                                                 ", ".join(i["id"] for i in items)))
        sys.exit(2)
    verdict = done_verdict(it, _load_anchor())
    if verdict:
        print("blocked")
        log(verdict)
        sys.exit(12)
    anchor = _load_anchor()
    it["state"] = "done"
    it["done_at"] = _now()
    it["evidence"] = "; ".join("%s: %s" % (c.get("id"), c.get("evidence") or "-")
                               for c in anchor.get("criteria", []))
    _save(master, items)
    log("%s done · evidence copied from the verified anchor" % it["id"])
    print("done")


def cmd_skip(opts):
    master, items, err = _load()
    if err or master is None:
        print("backlog: none frozen — run `init` first")
        sys.exit(2)
    it = _find(items, (opts.get("id") or "").strip())
    if not it:
        print("backlog: no item %r (have %s)" % (opts.get("id"),
                                                 ", ".join(i["id"] for i in items)))
        sys.exit(2)
    reason = opts.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        print("backlog: refusing to skip — --reason is required (quarantine needs a receipt)")
        sys.exit(2)
    it["state"] = "skipped"
    it["reason"] = reason.strip()
    _save(master, items)
    log("%s skipped (quarantined) — it no longer blocks the dry condition" % it["id"])
    print("skipped")


def cmd_status(opts):
    master, items, err = _load()
    if err or master is None:
        print("backlog: none frozen%s" % ((" (%s)" % err) if err else ""))
        return
    counts = {s: sum(1 for it in items if it.get("state") == s) for s in STATES}
    print("backlog: goal_fp=%s · frozen=%s · genesis=%s · %s" % (
        master.get("goal_fp"), master.get("frozen_at"), master.get("genesis"),
        " ".join("%s=%d" % (s, counts[s]) for s in STATES)))
    for it in items:
        log("[%-7s] %-4s %s%s%s" % (
            it.get("state"), it.get("id"), it.get("goal"),
            (" deps=%s" % ",".join(it["depends_on"])) if it.get("depends_on") else "",
            (" <%s>" % it["reason"]) if it.get("reason") else ""))


def cmd_check(opts):
    master, items, err = _load()
    goal_now = opts.get("goal") if isinstance(opts.get("goal"), str) else None
    if err:
        v = {"verdict": "DRIFT", "reason": err}
    elif master is None:
        v = {"verdict": "DRIFT", "reason": "no backlog frozen — run `init` first"}
    elif goal_now is not None and goal_fingerprint(goal_now) != master.get("goal_fp"):
        v = {"verdict": "DRIFT",
             "reason": "the goal worked this turn != the frozen master goal (re-init with "
                       "--force if the body of work genuinely changed)"}
    else:
        bad = validate_items([{"goal": it.get("goal"), "acs": it.get("acs"),
                               "id": it.get("id"), "depends_on": it.get("depends_on")}
                              for it in items])
        v = ({"verdict": "DRIFT", "reason": bad} if bad
             else {"verdict": "BACKLOG_OK", "reason": "frozen decomposition intact"})
    if opts.get("json"):
        print(json.dumps(v, indent=2, ensure_ascii=False))
    else:
        print(v["verdict"])
        log(v["reason"])
    if opts.get("exit-code") and v["verdict"] == "DRIFT":
        sys.exit(11)


def cmd_selftest(_opts):
    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-32s got=%r want=%r" % ("ok" if ok else "XX", name, got, want))

    # fingerprint is the anchor's own function — item fp ≡ anchor fp byte-for-byte
    chk("fp.anchor_equiv", goal_fingerprint("Build  THE api"), goal_fingerprint("build the api"))
    chk("fp.distinct", goal_fingerprint("a") != goal_fingerprint("b"), True)

    # freeze refuses an empty list and a zero-AC item; accepts a valid decomposition
    chk("init.refuse_empty", validate_items([]) is not None, True)
    chk("init.refuse_zero_ac",
        validate_items([{"goal": "x", "acs": []}]) is not None, True)
    chk("init.refuse_unknown_dep",
        validate_items([{"goal": "x", "acs": ["a"], "depends_on": ["T9"]}]) is not None, True)
    chk("init.refuse_cycle",
        validate_items([{"id": "A", "goal": "x", "acs": ["a"], "depends_on": ["B"]},
                        {"id": "B", "goal": "y", "acs": ["b"], "depends_on": ["A"]}]) is not None,
        True)
    chk("init.accept_valid",
        validate_items([{"goal": "x", "acs": ["a"]}, {"goal": "y", "acs": ["b"],
                        "depends_on": ["T1"]}]), None)

    # genesis classifier on synthetic listings
    chk("genesis.empty", classify_genesis([]), True)
    chk("genesis.readme_only", classify_genesis(["README.md", "LICENSE", "docs/notes.md"]), True)
    chk("genesis.one_py", classify_genesis(["README.md", "src/app.py"]), False)
    chk("genesis.dotfile_ignored", classify_genesis([".env", ".github/ci.yml"]), True)

    # genesis ordering: scaffold moves to T1, every other item depends on it
    raw = [{"goal": "feature", "acs": ["f"]},
           {"goal": "bootstrap", "acs": ["s"], "tags": ["scaffold"]}]
    ordered, err = order_for_genesis(raw)
    chk("genesis.scaffold_first", (err, ordered[0]["goal"]), (None, "bootstrap"))
    master, items = freeze_items("greenfield", ordered, True)
    chk("genesis.ids", [it["id"] for it in items], ["T1", "T2"])
    chk("genesis.deps_injected", items[1]["depends_on"], ["T1"])
    chk("genesis.refuse_no_scaffold",
        order_for_genesis([{"goal": "x", "acs": ["a"]}])[1] is not None, True)

    # next honors depends_on, re-prints claimed, and reports dry
    _, items = freeze_items("g", [{"id": "T1", "goal": "one", "acs": ["a"]},
                                  {"id": "T2", "goal": "two", "acs": ["b"],
                                   "depends_on": ["T1"]}], False)
    chk("next.first_ready", pick_next(items)[1]["id"], "T1")
    items[0]["state"] = "claimed"
    chk("next.reprints_claimed", pick_next(items)[0], "claimed")
    items[0]["state"] = "done"
    chk("next.dep_unlocked", pick_next(items)[1]["id"], "T2")
    items[1]["state"] = "skipped"
    chk("next.dry", pick_next(items)[0], "empty")

    # done verdict: refuses no-anchor / wrong-fp / pending-AC, passes a verified anchor
    item = {"goal_fp": goal_fingerprint("one")}
    chk("done.refuse_no_anchor", done_verdict(item, {}) is not None, True)
    chk("done.refuse_wrong_fp",
        done_verdict(item, {"goal_fp": goal_fingerprint("two"), "criteria": []}) is not None, True)
    pending = {"goal_fp": goal_fingerprint("one"),
               "criteria": [{"id": "AC1", "status": "pending"}]}
    chk("done.refuse_pending_ac", done_verdict(item, pending) is not None, True)
    verified = {"goal_fp": goal_fingerprint("one"),
                "criteria": [{"id": "AC1", "status": "done", "evidence": "e"}]}
    chk("done.pass_verified", done_verdict(item, verified), None)

    # re-init merge preserves per-item progress by normalized goal text
    _, old = freeze_items("g", [{"goal": "one", "acs": ["a"]}], False)
    old[0]["state"] = "done"
    old[0]["evidence"] = "AC1: e"
    _, new = freeze_items("g", [{"goal": "one", "acs": ["a"]},
                                {"goal": "brand new", "acs": ["b"]}], False)
    merged = merge_items(old, new)
    chk("merge.preserve_done", (merged[0]["state"], merged[0]["evidence"]), ("done", "AC1: e"))
    chk("merge.new_open", merged[1]["state"], "open")

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


def _parse(args):
    """Parse --k v / --flag (same hand-rolled shape as the sibling workers)."""
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
    # --describe-cli: emit JSON spec of accepted verbs + flags
    if argv[0] == "--describe-cli":
        print(json.dumps({
            "verbs": ["init", "genesis", "next", "done", "skip", "status", "check", "selftest"],
            "flags": ["--goal", "--items-file", "--stdin", "--genesis", "--force", "--root",
                      "--id", "--reason", "--json", "--format", "--exit-code", "--help"],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    {"init": cmd_init, "genesis": cmd_genesis, "next": cmd_next, "done": cmd_done,
     "skip": cmd_skip, "status": cmd_status, "check": cmd_check, "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: init genesis next done skip "
                               "status check selftest" % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()
