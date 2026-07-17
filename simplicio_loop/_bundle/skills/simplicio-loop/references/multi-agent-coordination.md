# Multi-agent coordination: when every issue is already claimed (full detail)

Referenced from `SKILL.md` § Phase 0 / triage. This file covers what a session does when
`scripts/coordinator.py decide` (see `references/bound-operators.md` neighbours it conceptually,
but coordination across SESSIONS, not operators, is the concern here) says every open issue is
`DEFER_ACTIVE_CLAIM` or `RECLAIM_STALE` isn't yet due — i.e. nothing is safely OWN-able right now.

**This is not "nothing to do."** With multiple concurrent loop sessions/worktrees active against
the same repo (observed directly: issues #466-#469 each carried a live claim from a sibling
session while this session worked), the highest-leverage thing left to do is **review the open PRs
against this repo's own bar** — instead of duplicating claimed work or idling.

## The two-step check, every triage pass

1. **`python3 scripts/coordinator.py survey --repo <owner/repo> --issues <n1,n2,...> | python3
   scripts/coordinator.py decide --self-branch <this-branch>`** — one action per issue: `OWN` /
   `CONTINUE_OWN` / `DEFER_ACTIVE_CLAIM` / `RECLAIM_STALE` / `VERIFY_PARTIAL`, plus a
   `duplicate_risk` flag. Only `OWN`/`CONTINUE_OWN`/`RECLAIM_STALE` license starting or continuing
   work on that issue in THIS session.
2. **If every issue comes back `DEFER_ACTIVE_CLAIM`** (or the queue is otherwise empty of ownable
   work): switch to **reviewer mode** — `gh pr list --state open` and, for each, run:
   ```bash
   python3 scripts/pr_dod_review.py check --repo <owner/repo> --pr <N> --post
   ```
   This posts a mechanical verdict comment: which of CLAUDE.md's 7 DoD dimensions (implementação,
   testes unitários, testes de integração, testes de sistema, testes de regressão, benchmark de
   performance, cobertura mínima) are addressed in the PR body vs. missing, and which of the
   underlying issue's frozen `- [ ]` acceptance-criteria lines remain unresolved. `GAPS_FOUND` means
   the comment names EXACTLY what the claiming agent still needs to add — never a vague "LGTM" or a
   vague "needs work."

## What "review" means here (and what it doesn't)

- **Is** a mechanical, text-level check: keyword presence for each DoD dimension, `- [ ]`/`- [x]`
  extraction from the issue body, evidence-phrase proximity for AC resolution. Reproducible by
  anyone re-running the same command against the same PR/issue state.
- **Is not** a substitute for actually running the other session's test suite, and not a claim that
  a `COMPLIANT` verdict means the code is correct — only that the PR's OWN description clears the
  documented bar. A `COMPLIANT` mechanical verdict with obviously wrong logic is still a real bug;
  file it as a normal review finding, don't let the tool's green check silence a real read of the
  diff when you have time to do one.
- A PR that merges an "MVP slice" against a multi-week epic issue is expected to leave most of that
  issue's AC checklist `unresolved_acs` — that's not a defect in the tool, it's the correct signal
  that the epic issue must stay open and the remaining ACs need a follow-up item, exactly the
  discipline `references/triage-verify-detail.md` already requires for partial delivery
  (`VERIFY_PARTIAL` in the coordinator's own vocabulary).

## Why this matters at this repo's current scale

Idling because "my issues are all claimed" wastes exactly the wall-clock window that could instead
catch a merge-race regression (this session found two: PR #473's dropped `_atomic_write` line, and
the subsequent squash-merge race that reintroduced it onto `main`) or flag that a merged PR's own
issue is nowhere near actually done. Reviewing is real, in-scope work — not a consolation prize for
losing a claim race.
