#!/usr/bin/env python3
"""simplicio-loop — agent replication MVP core (issue #469, elastic replication).

Issue #469 asks for a large "Elastic Replication" coordinator: dynamically request
hedged/speculative agent replicas, run diverse strategies in parallel, select a verified
winner, and cancel the losers with exactly-once mutation guarantees. That full epic needs
live subagent spawning, worktree isolation wiring, and GitHub delivery -- all tracked in a
follow-up issue.

This worker ships only the genuine, working MVP CORE the rest of the epic builds on:

  1. **Admission control** -- `decide_admission()`, a pure function deciding whether (and how
     many) replicas a request is allowed, from simple explainable rules mirroring the issue's
     "quando replicar" / "quando nao replicar" list: deny on insufficient slots/budget, cap
     weakly-justified (`idle_capacity`-only) requests to +1 replica instead of the full ask,
     otherwise admit the min of what's requested/allowed/affordable.
  2. **Winner selection** -- `select_winner()`, a pure function implementing
     "first_verified_candidate_wins" (the anti-pattern the issue explicitly calls out is
     first_response_wins): the winner is the EARLIEST-VERIFIED candidate, never the
     earliest-responded one, tie-broken by lowest cost.
  3. **Loser cancellation** -- `cancel_losers()`, a pure function returning every replica_id
     except the winner's, refusing (not raising) if the winner_id is not among the candidates.

No live subagent spawning, no worktree isolation, no GitHub delivery here -- this is the
admission + selection logic the future coordinator will call.

State: none persisted by this worker (pure decision functions); the CLI is a thin wrapper for
manual/CI invocation and `--json` piping into a real coordinator.

Verbs:
  request        Build a ReplicationRequest from flags, run admission, print the decision.
  select-winner  Read candidates from --candidates-file (JSON list), print the winner verdict.
  cancel-losers  Read candidates from --candidates-file, print the replica_ids to cancel.
  selftest       Prove admission/selection/cancellation deterministically -- no files, no I/O.

Usage:
    python3 scripts/agent_replication.py request --mode replica_diverse_strategy \\
        --reason-code slow_p95 --requested 3 --min-replicas 1 --max-replicas 3 \\
        --token-budget 900 --available-slots 4 --available-budget 900 --json
    python3 scripts/agent_replication.py select-winner --candidates-file candidates.json --json
    python3 scripts/agent_replication.py cancel-losers --candidates-file candidates.json \\
        --winner-id r2 --json
    python3 scripts/agent_replication.py selftest
"""
import json
import math
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII -- force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODES = ("shard", "stage_fanout", "replica_same_strategy", "replica_diverse_strategy",
          "redundant_verification", "hot_standby", "portfolio")
REASON_CODES = ("slow_p95", "no_progress", "failure_fingerprint_repeat", "high_variance",
                "critical_path_block", "low_confidence", "idle_capacity", "sla_risk")

# Cost (in the same unit as token_budget/available_budget) charged per admitted replica.
# A placeholder constant until the real coordinator wires actual per-runtime pricing --
# kept as a single named constant so the whole admission math updates from one spot.
PER_REPLICA_COST = 100

# Reason codes considered a "real signal" -- i.e. NOT the weak idle_capacity-only case that
# gets capped. Mirrors the issue's "nao replicar automaticamente quando... custo excede ganho
# estimado" guidance: idle capacity alone is not, by itself, sufficient justification for the
# full requested replica count.
STRONG_REASON_CODES = frozenset(REASON_CODES) - {"idle_capacity"}


def log(msg):
    print("  " + msg)


# ----- pure helpers (selftest exercises these directly, no I/O) -----------------------------------

def make_request(request_id, task_id, mode, reason_code, requested_replicas, min_replicas,
                  max_replicas, deadline_s=None, token_budget=0):
    """Build (and lightly validate) a ReplicationRequest dict."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    if reason_code not in REASON_CODES:
        raise ValueError("reason_code must be one of %s" % ", ".join(REASON_CODES))
    return {
        "request_id": request_id,
        "task_id": task_id,
        "mode": mode,
        "reason_code": reason_code,
        "requested_replicas": int(requested_replicas),
        "min_replicas": int(min_replicas),
        "max_replicas": int(max_replicas),
        "deadline_s": float(deadline_s) if deadline_s is not None else None,
        "token_budget": int(token_budget),
    }


def decide_admission(request, available_slots, available_budget,
                      per_replica_cost=PER_REPLICA_COST):
    """Pure, unit-testable admission decision. No I/O.

    Rules (mirrors issue #469's "quando replicar" / "quando nao replicar" list):
      1. Deny if there aren't even `min_replicas` slots available.
      2. Deny if there isn't even `min_replicas * per_replica_cost` budget available.
      3. If the ONLY justification is `idle_capacity` (no stronger reason_code accompanies
         it), cap the admitted count at `min_replicas + 1` extra replica rather than the full
         requested amount -- weak justification gets a small hedge, not a blank check.
      4. Otherwise admit min(requested, max_replicas, available_slots,
         floor(available_budget / per_replica_cost)) replicas.

    Returns {"admitted": bool, "replicas": int, "reason": str}.
    """
    min_r = request["min_replicas"]
    max_r = request["max_replicas"]
    requested = request["requested_replicas"]
    reason_code = request["reason_code"]

    if available_slots < min_r:
        return {"admitted": False, "replicas": 0, "reason": "insufficient_slots"}

    if available_budget < min_r * per_replica_cost:
        return {"admitted": False, "replicas": 0, "reason": "insufficient_budget"}

    affordable = int(math.floor(available_budget / float(per_replica_cost)))
    cap = min(requested, max_r, available_slots, affordable)
    cap = max(cap, 0)

    if reason_code == "idle_capacity":
        # Weak signal alone: cap replicas at min_replicas + 1 extra, never the full ask.
        weak_cap = min(cap, max(min_r, 1) + 1)
        weak_cap = max(weak_cap, 0)
        if weak_cap < min_r:
            return {"admitted": False, "replicas": 0,
                    "reason": "insufficient_slots_or_budget_for_min_replicas"}
        return {"admitted": True, "replicas": weak_cap,
                "reason": "admitted_capped_weak_justification"}

    if cap < min_r:
        return {"admitted": False, "replicas": 0,
                "reason": "insufficient_slots_or_budget_for_min_replicas"}

    return {"admitted": True, "replicas": cap, "reason": "admitted"}


def select_winner(candidates):
    """Pure: 'first_verified_candidate_wins', NOT first_response_wins.

    Winner = candidate with the EARLIEST verified_at among verified:true candidates.
    Ties on verified_at break by lowest cost. No verified candidate -> winner None.
    """
    verified = [c for c in (candidates or []) if c.get("verified")]
    if not verified:
        return {"winner": None, "reason": "no_verified_candidate"}
    best = min(verified, key=lambda c: (c["verified_at"], c.get("cost", 0)))
    return {"winner": best["replica_id"], "reason": "first_verified_candidate_wins",
            "verified_at": best["verified_at"], "cost": best.get("cost", 0)}


def cancel_losers(candidates, winner_id):
    """Pure: replica_ids to cancel = all candidates except winner_id.

    Refuses (returns an error dict, never raises) if winner_id isn't among the candidates.
    """
    ids = [c.get("replica_id") for c in (candidates or [])]
    if winner_id not in ids:
        return {"ok": False, "error": "unknown_winner_id", "cancel": []}
    return {"ok": True, "error": None, "cancel": [i for i in ids if i != winner_id]}


# ----- CLI ------------------------------------------------------------------------------------

def cmd_request(opts):
    try:
        req = make_request(
            request_id=opts.get("request-id") or "req-1",
            task_id=opts.get("task-id") or "task-1",
            mode=opts.get("mode") or "",
            reason_code=opts.get("reason-code") or "",
            requested_replicas=opts.get("requested") or 0,
            min_replicas=opts.get("min-replicas") or 0,
            max_replicas=opts.get("max-replicas") or 0,
            deadline_s=opts.get("deadline-s"),
            token_budget=opts.get("token-budget") or 0,
        )
    except ValueError as exc:
        print(json.dumps({"admitted": False, "error": str(exc)}) if opts.get("json")
              else "agent_replication: %s" % exc)
        sys.exit(2)

    available_slots = int(opts.get("available-slots") or 0)
    available_budget = int(opts.get("available-budget") or 0)
    decision = decide_admission(req, available_slots, available_budget)

    if opts.get("json"):
        print(json.dumps({"request": req, "decision": decision}, ensure_ascii=False))
    else:
        log("request %s: mode=%s reason=%s requested=%d" % (
            req["request_id"], req["mode"], req["reason_code"], req["requested_replicas"]))
        print("admitted" if decision["admitted"] else "denied")
        log("replicas=%d reason=%s" % (decision["replicas"], decision["reason"]))
    sys.exit(0 if decision["admitted"] else 1)


def _load_candidates(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_select_winner(opts):
    path = opts.get("candidates-file")
    if not path:
        print("agent_replication: --candidates-file is required")
        sys.exit(2)
    candidates = _load_candidates(path)
    verdict = select_winner(candidates)
    if opts.get("json"):
        print(json.dumps(verdict, ensure_ascii=False))
    else:
        if verdict["winner"] is None:
            print("no winner")
            log(verdict["reason"])
        else:
            print("winner: %s" % verdict["winner"])
            log("verified_at=%s cost=%s" % (verdict["verified_at"], verdict["cost"]))
    sys.exit(0 if verdict["winner"] is not None else 1)


def cmd_cancel_losers(opts):
    path = opts.get("candidates-file")
    winner_id = opts.get("winner-id")
    if not path or not winner_id:
        print("agent_replication: --candidates-file and --winner-id are required")
        sys.exit(2)
    candidates = _load_candidates(path)
    result = cancel_losers(candidates, winner_id)
    if opts.get("json"):
        print(json.dumps(result, ensure_ascii=False))
    else:
        if not result["ok"]:
            print("error: %s" % result["error"])
        else:
            print("cancel: %s" % ", ".join(result["cancel"]) if result["cancel"] else "cancel: (none)")
    sys.exit(0 if result["ok"] else 2)


def cmd_selftest(_opts):
    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-40s got=%r want=%r" % ("ok" if ok else "XX", name, got, want))

    # -- admission: insufficient slots ---------------------------------------------------------
    req = make_request("r1", "t1", "replica_diverse_strategy", "slow_p95", 3, 2, 3,
                        token_budget=1000)
    d = decide_admission(req, available_slots=1, available_budget=1000)
    chk("admission.insufficient_slots", d["admitted"], False)
    chk("admission.insufficient_slots.reason", d["reason"], "insufficient_slots")

    # -- admission: insufficient budget --------------------------------------------------------
    d = decide_admission(req, available_slots=3, available_budget=50)
    chk("admission.insufficient_budget", d["admitted"], False)
    chk("admission.insufficient_budget.reason", d["reason"], "insufficient_budget")

    # -- admission: idle_capacity alone is capped ------------------------------------------------
    req_idle = make_request("r2", "t1", "portfolio", "idle_capacity", 5, 1, 5,
                             token_budget=1000)
    d = decide_admission(req_idle, available_slots=5, available_budget=1000)
    chk("admission.idle_capacity_capped", d["admitted"], True)
    chk("admission.idle_capacity_capped.replicas_lt_requested", d["replicas"] < 5, True)
    chk("admission.idle_capacity_capped.replicas", d["replicas"], 2)  # min_replicas(1)+1

    # -- admission: normal case admits the expected min() -----------------------------------------
    req_norm = make_request("r3", "t1", "replica_diverse_strategy", "high_variance", 3, 1, 3,
                             token_budget=1000)
    d = decide_admission(req_norm, available_slots=10, available_budget=1000)
    chk("admission.normal_admits", d["admitted"], True)
    chk("admission.normal_admits.replicas", d["replicas"], 3)  # min(3,3,10,10)=3

    d = decide_admission(req_norm, available_slots=2, available_budget=1000)
    chk("admission.normal_slots_bound", d["replicas"], 2)  # slots binds

    # -- winner selection: THE anti-pattern test --------------------------------------------------
    # A candidate that responded FIRST but was never verified must lose to a LATER candidate
    # that WAS verified -- first_verified_candidate_wins, not first_response_wins.
    candidates = [
        {"replica_id": "fast_unverified", "responded_at": 1.0, "verified": False,
         "verified_at": None, "cost": 1.0},
        {"replica_id": "slower_verified", "responded_at": 5.0, "verified": True,
         "verified_at": 6.0, "cost": 2.0},
    ]
    v = select_winner(candidates)
    chk("winner.first_response_does_not_win", v["winner"], "slower_verified")
    chk("winner.reason", v["reason"], "first_verified_candidate_wins")

    # -- winner selection: tie-break by lowest cost -----------------------------------------------
    tie = [
        {"replica_id": "cheap", "responded_at": 1.0, "verified": True, "verified_at": 3.0,
         "cost": 1.0},
        {"replica_id": "pricey", "responded_at": 2.0, "verified": True, "verified_at": 3.0,
         "cost": 5.0},
    ]
    chk("winner.tie_break_cost", select_winner(tie)["winner"], "cheap")

    # -- winner selection: no verified candidate ---------------------------------------------------
    none_verified = [{"replica_id": "a", "responded_at": 1.0, "verified": False,
                       "verified_at": None, "cost": 1.0}]
    v = select_winner(none_verified)
    chk("winner.none_verified", v["winner"], None)
    chk("winner.none_verified.reason", v["reason"], "no_verified_candidate")
    chk("winner.empty_list", select_winner([])["winner"], None)

    # -- cancel_losers -------------------------------------------------------------------------
    c = cancel_losers(candidates, "slower_verified")
    chk("cancel.ok", c["ok"], True)
    chk("cancel.excludes_winner", c["cancel"], ["fast_unverified"])

    bad = cancel_losers(candidates, "does_not_exist")
    chk("cancel.unknown_winner_denied", bad["ok"], False)
    chk("cancel.unknown_winner_error", bad["error"], "unknown_winner_id")
    chk("cancel.unknown_winner_no_raise", bad["cancel"], [])

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


def _parse(args):
    """Parse --k v / --flag; string values only (CLI is a thin manual/CI wrapper)."""
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
    if argv[0] == "--describe-cli":
        print(json.dumps({
            "verbs": ["request", "select-winner", "cancel-losers", "selftest"],
            "flags": ["--mode", "--reason-code", "--requested", "--min-replicas",
                      "--max-replicas", "--token-budget", "--deadline-s", "--request-id",
                      "--task-id", "--available-slots", "--available-budget",
                      "--candidates-file", "--winner-id", "--json"],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    {"request": cmd_request, "select-winner": cmd_select_winner,
     "cancel-losers": cmd_cancel_losers, "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: request select-winner "
                               "cancel-losers selftest" % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()
