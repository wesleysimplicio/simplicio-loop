#!/usr/bin/env python3
"""simplicio-loop — workflow topology: semantic DAG differ + static validator (MVP core, #468).

GitHub issue #468 ("[P1][Adaptive Architecture]") asks for a large system where a coordinator
proposes new pipeline stages/agents/gates via RFCs, simulates them, canaries and
promotes/rolls back. This worker is a deliberate MVP CORE slice of that epic — NOT the full
system: no GitHub RFC creation, no canary/promotion/rollback machinery, no live coordinator
(tracked in a follow-up issue). What it DOES ship, genuinely working:

  1. A manifest format describing a pipeline as a stage DAG (`load_manifest`).
  2. A static `validate()` that flags cycles, missing dependencies, orphan stages, and
     duplicate stage ids — the guardrail a coordinator would need before ever proposing a
     change to the live pipeline.
  3. A semantic `diff()` between two manifests — the "semantic DAG differ" the issue asks for,
     at MVP scope: added/removed stages + changed dependency sets + whether stage order shifted.
  4. A `critical_path()` calculator — the longest stage-cost chain, the "critical path" signal
     the issue asks for, at minimal (unit-cost) scope.

Manifest shape (JSON):
    {"stages": [{"id": "survey", "depends_on": []},
                {"id": "decide", "depends_on": ["survey"]}, ...]}

Verbs:
  validate       Load a manifest and print/return validation issues. --exit-code -> exit 1 if
                 any issue was found.
  diff           Semantic DAG diff between OLD.json and NEW.json.
  critical-path  Longest dependency chain (unit cost per stage).
  selftest       Build small in-memory/temp-dir manifests (with and without a cycle) and
                 exercise validate/diff/critical-path. Prints PASS/FAIL, exits 0/1.

Usage:
    python3 scripts/workflow_topology.py validate examples/loop_pipeline_topology.json --json
    python3 scripts/workflow_topology.py validate manifest.json --exit-code
    python3 scripts/workflow_topology.py diff old.json new.json --json
    python3 scripts/workflow_topology.py critical-path manifest.json --json
    python3 scripts/workflow_topology.py selftest
"""
import json
import os
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def log(msg):
    print("  " + msg)


# ----- pure helpers (unit-testable, no I/O) --------------------------------------------------

def load_manifest(path):
    """Load a pipeline manifest JSON file -> dict with a 'stages' list. Raises on bad JSON /
    missing file (callers decide how to report that — CLI commands catch and exit 2)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        raise ValueError("manifest must be a JSON object with a 'stages' list")
    return data


def _stage_ids(manifest):
    return [s.get("id") for s in manifest.get("stages", [])]


def _issue(code, message, **extra):
    d = {"code": code, "message": message}
    d.update(extra)
    return d


def validate(manifest):
    """Static validator: cycles, missing deps, orphan stages, duplicate ids.

    Returns {"valid": bool, "issues": [{"code", "message", ...}]}.
    """
    stages = manifest.get("stages", []) if isinstance(manifest, dict) else []
    issues = []

    # duplicate stage ids
    seen = {}
    for s in stages:
        sid = s.get("id")
        seen[sid] = seen.get(sid, 0) + 1
    dupes = sorted([sid for sid, n in seen.items() if n > 1])
    for sid in dupes:
        issues.append(_issue("duplicate_stage", "stage id %r is declared more than once" % sid,
                             stage=sid))

    all_ids = set(seen.keys())
    # build depends_on graph, deduping stage entries by first occurrence for edge purposes
    deps = {}
    for s in stages:
        sid = s.get("id")
        if sid in deps:
            continue  # duplicate already reported above
        deps[sid] = list(s.get("depends_on") or [])

    # missing dependency: a stage depends on an id that doesn't exist
    for sid, dlist in deps.items():
        for d in dlist:
            if d not in all_ids:
                issues.append(_issue("missing_dependency",
                                     "stage %r depends on unknown stage %r" % (sid, d),
                                     stage=sid, missing=d))

    # cycle detection (DFS, only walking edges to known ids to avoid double-reporting missing deps)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in deps}
    cyclic_stages = set()

    def visit(sid, stack):
        if color.get(sid) == BLACK:
            return
        if color.get(sid) == GRAY:
            # found a cycle: everything from sid's first occurrence in stack onward
            if sid in stack:
                idx = stack.index(sid)
                for s in stack[idx:]:
                    cyclic_stages.add(s)
            return
        color[sid] = GRAY
        stack.append(sid)
        for d in deps.get(sid, []):
            if d in deps:
                visit(d, stack)
        stack.pop()
        color[sid] = BLACK

    for sid in list(deps.keys()):
        if color.get(sid) == WHITE:
            visit(sid, [])
    for sid in sorted(cyclic_stages):
        issues.append(_issue("cycle", "stage %r participates in a dependency cycle" % sid,
                             stage=sid))

    # orphan stage: unreachable from any root (a stage with empty depends_on), only flagged
    # when at least one stage actually HAS a non-empty depends_on (avoid false positives on
    # trivial single-stage / all-root manifests).
    has_edges = any(dlist for dlist in deps.values())
    if has_edges:
        roots = [sid for sid, dlist in deps.items() if not dlist]
        reachable = set(roots)
        frontier = list(roots)
        # forward adjacency: sid -> stages that depend on it
        children = {sid: [] for sid in deps}
        for sid, dlist in deps.items():
            for d in dlist:
                if d in children:
                    children[d].append(sid)
        while frontier:
            cur = frontier.pop()
            for child in children.get(cur, []):
                if child not in reachable:
                    reachable.add(child)
                    frontier.append(child)
        for sid in deps:
            if sid not in reachable and sid not in cyclic_stages:
                issues.append(_issue("orphan_stage",
                                     "stage %r is unreachable from any root stage "
                                     "(no depends_on path connects it)" % sid, stage=sid))

    return {"valid": len(issues) == 0, "issues": issues}


def diff(old_manifest, new_manifest):
    """Semantic DAG diff between two manifests.

    Returns {"added_stages", "removed_stages", "changed_dependencies", "reordered"}.
    """
    old_stages = old_manifest.get("stages", []) if isinstance(old_manifest, dict) else []
    new_stages = new_manifest.get("stages", []) if isinstance(new_manifest, dict) else []
    old_ids = [s.get("id") for s in old_stages]
    new_ids = [s.get("id") for s in new_stages]
    old_set, new_set = set(old_ids), set(new_ids)

    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)

    old_deps = {s.get("id"): list(s.get("depends_on") or []) for s in old_stages}
    new_deps = {s.get("id"): list(s.get("depends_on") or []) for s in new_stages}

    changed = []
    for sid in sorted(old_set & new_set):
        od = old_deps.get(sid, [])
        nd = new_deps.get(sid, [])
        if sorted(od) != sorted(nd):
            changed.append({"stage": sid, "old_depends_on": od, "new_depends_on": nd})

    # reordered: the relative order of stages common to both manifests differs
    common = [sid for sid in old_ids if sid in new_set]
    common_new_order = [sid for sid in new_ids if sid in old_set]
    reordered = common != common_new_order

    return {
        "added_stages": added,
        "removed_stages": removed,
        "changed_dependencies": changed,
        "reordered": reordered,
    }


def critical_path(manifest):
    """Longest dependency chain (list of stage ids), unit cost per stage.

    Cyclic manifests: cycle members are excluded from consideration for the chains they
    participate in (validate() should be run first / issues surfaced) — this function does not
    itself raise on a cycle, it simply won't produce infinite recursion (memoized DFS with a
    visiting-guard).
    """
    stages = manifest.get("stages", []) if isinstance(manifest, dict) else []
    deps = {}
    for s in stages:
        sid = s.get("id")
        if sid not in deps:
            deps[sid] = list(s.get("depends_on") or [])

    memo = {}
    visiting = set()

    def longest_chain(sid):
        if sid in memo:
            return memo[sid]
        if sid in visiting or sid not in deps:
            return []  # cycle guard / unknown dep — stop here
        visiting.add(sid)
        best = []
        for d in deps.get(sid, []):
            chain = longest_chain(d)
            if len(chain) > len(best):
                best = chain
        result = best + [sid]
        visiting.discard(sid)
        memo[sid] = result
        return result

    longest = []
    for sid in deps:
        chain = longest_chain(sid)
        if len(chain) > len(longest):
            longest = chain
    return longest


# ----- CLI -------------------------------------------------------------------------------------

def _parse(args):
    opts = {}
    positional = []
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
            positional.append(a)
            i += 1
    return opts, positional


def cmd_validate(opts, positional):
    if not positional:
        print("workflow_topology: validate requires a manifest path")
        sys.exit(2)
    path = positional[0]
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError) as e:
        print("workflow_topology: failed to load %r: %s" % (path, e))
        sys.exit(2)
    result = validate(manifest)
    if opts.get("json"):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("valid: %s" % result["valid"])
        for issue in result["issues"]:
            log("[%s] %s" % (issue["code"], issue["message"]))
        if result["valid"]:
            log("no issues found")
    if opts.get("exit-code") and not result["valid"]:
        sys.exit(1)


def cmd_diff(opts, positional):
    if len(positional) < 2:
        print("workflow_topology: diff requires OLD.json NEW.json")
        sys.exit(2)
    old_path, new_path = positional[0], positional[1]
    try:
        old_manifest = load_manifest(old_path)
        new_manifest = load_manifest(new_path)
    except (OSError, ValueError) as e:
        print("workflow_topology: failed to load manifest: %s" % e)
        sys.exit(2)
    result = diff(old_manifest, new_manifest)
    if opts.get("json"):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("added: %s" % (", ".join(result["added_stages"]) or "-"))
        print("removed: %s" % (", ".join(result["removed_stages"]) or "-"))
        if result["changed_dependencies"]:
            print("changed dependencies:")
            for c in result["changed_dependencies"]:
                log("%s: %s -> %s" % (c["stage"], c["old_depends_on"], c["new_depends_on"]))
        else:
            print("changed dependencies: -")
        print("reordered: %s" % result["reordered"])


def cmd_critical_path(opts, positional):
    if not positional:
        print("workflow_topology: critical-path requires a manifest path")
        sys.exit(2)
    path = positional[0]
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError) as e:
        print("workflow_topology: failed to load %r: %s" % (path, e))
        sys.exit(2)
    chain = critical_path(manifest)
    if opts.get("json"):
        print(json.dumps({"critical_path": chain, "length": len(chain)}, ensure_ascii=False))
    else:
        print("critical path (%d stages): %s" % (len(chain), " -> ".join(chain)))


def cmd_selftest(_opts, _positional):
    import tempfile

    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-32s got=%r want=%r" % ("ok" if ok else "XX", name, got, want))

    # --- validate: clean manifest ---
    clean = {"stages": [
        {"id": "survey", "depends_on": []},
        {"id": "decide", "depends_on": ["survey"]},
        {"id": "operate", "depends_on": ["decide"]},
    ]}
    v = validate(clean)
    chk("validate.clean", v["valid"], True)
    chk("validate.clean_no_issues", v["issues"], [])

    # --- validate: trivial single-stage manifest must NOT false-positive on orphan ---
    trivial = {"stages": [{"id": "solo", "depends_on": []}]}
    v_trivial = validate(trivial)
    chk("validate.trivial_no_orphan_fp", v_trivial["valid"], True)

    # --- validate: cycle ---
    cyclic = {"stages": [
        {"id": "a", "depends_on": ["c"]},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
    ]}
    v_cyc = validate(cyclic)
    chk("validate.cycle_detected", v_cyc["valid"], False)
    chk("validate.cycle_code", any(i["code"] == "cycle" for i in v_cyc["issues"]), True)

    # --- validate: missing dependency ---
    missing = {"stages": [{"id": "x", "depends_on": ["nope"]}]}
    v_miss = validate(missing)
    chk("validate.missing_dep", any(i["code"] == "missing_dependency" for i in v_miss["issues"]),
        True)

    # --- validate: duplicate stage ---
    dup = {"stages": [{"id": "x", "depends_on": []}, {"id": "x", "depends_on": []}]}
    v_dup = validate(dup)
    chk("validate.duplicate", any(i["code"] == "duplicate_stage" for i in v_dup["issues"]), True)

    # --- validate: orphan stage (a real, non-trivial graph with an unreachable extra stage) ---
    orphan = {"stages": [
        {"id": "root", "depends_on": []},
        {"id": "mid", "depends_on": ["root"]},
        {"id": "island", "depends_on": []},  # a second root is fine...
    ]}
    # make "island" NOT a root by giving it a depends_on that still leaves it unreachable
    orphan2 = {"stages": [
        {"id": "root", "depends_on": []},
        {"id": "mid", "depends_on": ["root"]},
        {"id": "ghost", "depends_on": ["ghost_parent"]},
        {"id": "ghost_parent", "depends_on": ["ghost"]},  # cyclic pair, isolated from root
    ]}
    v_orphan = validate(orphan)
    chk("validate.two_roots_ok", v_orphan["valid"], True)  # two roots isn't itself an orphan
    v_orphan2 = validate(orphan2)
    chk("validate.cyclic_island_flagged",
        any(i["code"] in ("cycle", "orphan_stage") for i in v_orphan2["issues"]), True)

    # --- diff ---
    old = {"stages": [
        {"id": "survey", "depends_on": []},
        {"id": "decide", "depends_on": ["survey"]},
    ]}
    new = {"stages": [
        {"id": "survey", "depends_on": []},
        {"id": "decide", "depends_on": ["survey"]},
        {"id": "operate", "depends_on": ["decide"]},
    ]}
    d = diff(old, new)
    chk("diff.added", d["added_stages"], ["operate"])
    chk("diff.removed", d["removed_stages"], [])
    chk("diff.reordered", d["reordered"], False)

    new_removed = {"stages": [{"id": "survey", "depends_on": []}]}
    d2 = diff(old, new_removed)
    chk("diff.removed_detected", d2["removed_stages"], ["decide"])

    changed_dep = {"stages": [
        {"id": "survey", "depends_on": []},
        {"id": "decide", "depends_on": ["survey", "triage"], "extra": 1},
        {"id": "triage", "depends_on": []},
    ]}
    old_for_change = {"stages": [
        {"id": "survey", "depends_on": []},
        {"id": "decide", "depends_on": ["survey"]},
        {"id": "triage", "depends_on": []},
    ]}
    d3 = diff(old_for_change, changed_dep)
    chk("diff.changed_dependency",
        d3["changed_dependencies"] == [{"stage": "decide", "old_depends_on": ["survey"],
                                        "new_depends_on": ["survey", "triage"]}], True)

    # --- critical path ---
    chain_manifest = {"stages": [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
        {"id": "d", "depends_on": ["a"]},
    ]}
    cp = critical_path(chain_manifest)
    chk("critical_path.longest", cp, ["a", "b", "c"])

    # --- CLI round-trip via a temp dir (exercises load_manifest + all three ops end-to-end) ---
    with tempfile.TemporaryDirectory() as td:
        clean_path = os.path.join(td, "clean.json")
        cyclic_path = os.path.join(td, "cyclic.json")
        with open(clean_path, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        with open(cyclic_path, "w", encoding="utf-8") as f:
            json.dump(cyclic, f)

        loaded_clean = load_manifest(clean_path)
        loaded_cyclic = load_manifest(cyclic_path)
        chk("cli.load_clean_valid", validate(loaded_clean)["valid"], True)
        chk("cli.load_cyclic_invalid", validate(loaded_cyclic)["valid"], False)
        chk("cli.diff_on_files", diff(loaded_clean, loaded_clean)["added_stages"], [])
        chk("cli.critical_path_on_file", critical_path(loaded_clean),
            ["survey", "decide", "operate"])

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    if argv[0] == "--describe-cli":
        print(json.dumps({
            "verbs": ["validate", "diff", "critical-path", "selftest"],
            "flags": ["--json", "--exit-code", "--help"],
        }))
        sys.exit(0)
    sub = argv[0]
    opts, positional = _parse(argv[1:])
    dispatch = {
        "validate": cmd_validate,
        "diff": cmd_diff,
        "critical-path": cmd_critical_path,
        "selftest": cmd_selftest,
    }
    if sub not in dispatch:
        print("unknown command '%s'. choices: validate diff critical-path selftest" % sub)
        sys.exit(2)
    dispatch[sub](opts, positional)


if __name__ == "__main__":
    main()
