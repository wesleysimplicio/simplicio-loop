---
name: simplicio-orient
description: Terminal-first execution — answer facts with the shell, never with the LLM. Use whenever a step needs a fact about the filesystem, git, processes, or system resources, or runs a build/test/lint/diff whose output would flood context. Substitutes deterministic shell/CLI calls for native LLM operations and clamps their output 60–90% (rtk-style) with a failure-safe tee cache, signatures-only reads, and an optional auto-rewrite hook. This is the token-economy spine of simplicio-tasks, usable standalone.
---

# simplicio-orient — terminal-first, token-frugal execution

The cheapest token is the one not spent. The terminal KNOWS facts exactly; the LLM
APPROXIMATES them expensively. This skill routes every step to the leanest substrate that
still completes it correctly, and clamps command output before it ever reaches context.

Credit: folds the disciplines of **rtk** (per-command output reduction, tee-on-failure,
signatures-only reads, auto-rewrite hook) and **caveman** (preserve code/paths byte-for-byte)
into the simplicio safety spine. It is the extracted, standalone form of `simplicio-tasks`
Step 1c.

## The one rule

> If the answer is a fact about the filesystem, git state, process state, or system
> resources — the terminal answers it exactly and cheaply. Use the terminal. The LLM is for
> reasoning; the terminal is for facts. **Execute commands for real — never reason about what a
> command "would return".**

## Execution priority

1. Host-runtime native command bound to `shell_exec` (structured, minimal tokens, cross-platform).
2. Shell/Bash tool call WITH output clamping (this skill's catalog).
3. NEVER: the LLM narrating a command's likely output.

## Terminal substitution table (use the terminal, not the LLM)

Detect platform once: `python3 -c "import platform; print(platform.system())"` →
`Windows | Darwin | Linux`. Prefer cross-platform tools (`git`, `gh`, `rg`, `python3`) so one
command works everywhere; fall back to OS-specific only when there is no alternative.

| What you need | ✅ Cross-platform (preferred) | Windows | Linux/macOS |
|---|---|---|---|
| File exists? | `python3 -c "import os,sys;sys.exit(0 if os.path.exists('<p>') else 1)"` | `Test-Path <p>` | `test -f <p>` |
| Find in code | `rg "<pat>" --json` | same | same |
| Count matches | `rg -c "<pat>" <file>` | same | same |
| List files by glob | `rg --files -g "*.ts"` | same | same |
| Current branch | `git rev-parse --abbrev-ref HEAD` | same | same |
| Ahead of main? | `git rev-list --count main..HEAD` | same | same |
| Files changed in branch | `git diff --name-only main...HEAD` | same | same |
| PR for branch | `gh pr list --head <b> --json number --jq ".[0].number"` | same | same |
| Issue state | `gh issue view N --json state --jq ".state"` | same | same |
| Open issue count | `gh issue list --state open --json number --jq "length"` | same | same |
| CPU cores | `python3 -c "import os;print(os.cpu_count())"` | `$env:NUMBER_OF_PROCESSORS` | `nproc` |
| Free disk GB | `python3 -c "import shutil;print(shutil.disk_usage('.').free//1024**3)"` | same | `df -BG .` |
| Extract JSON field | `python3 -c "import json,sys;print(json.load(sys.stdin)['<f>'])"` | same | `jq '.<f>'` |
| Today UTC | `python3 -c "from datetime import*;print(datetime.now(timezone.utc).date())"` | same | `date -u +%F` |
| Sort + dedup | `python3 -c "import sys;print('\n'.join(sorted(set(sys.stdin.read().split()))))"` | same | `sort -u` |
| Replace in file | bound `deterministic_edit` (host) | same | `sed -i` |

A raw `tsc --noEmit` costs ~2000 tokens to read; clamped (error lines only) costs ~80.
Terminal-first + the catalog below is the single
highest-leverage token rule.

## Output-reduction catalog (data table — drives clamp routing)

Consult BEFORE running. Each row `{pattern, recipe, exp-savings, SKIP-if}`. Clamp
highest-savings first; NEVER clamp a SKIP-if row (structured `--json`/`--jq` output, or a
write/confirm op). Tune per repo.

| command pattern | reduce recipe | exp. savings | skip-if |
|---|---|---|---|
| test/spec runner | success→`pass: N`; on fail keep ≤20 error lines | ~90% | piped to structured consumer |
| type/compile check | error lines only; clean→`ok` | ~80% | — |
| diff / show | stat + hunks only, drop context | ~80% | piped to structured consumer |
| lint | findings only; clean→`ok` | ~80% | — |
| add / commit / push | collapse to `ok <branch/sha>` | ~59% | — |
| PR / list view | counts + titles only | ~87% | `--json`/`--jq` present |
| package/image inventory | keep ≤50 rows | ~50% | — |
| format / passthrough | run raw | 0% | always |

## Signal-tiered truncation caps (one shared set)

Never flat "head N + tail N" — flat truncation over-cuts the errors the fix loop needs most.
ONE set referenced everywhere: `CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`,
`CAP_INVENTORY=50`. Always keep ERROR lines over surrounding context. A lowered cap is
underflow-safe: it falls back to the full cap rather than emptying a non-empty result.

## Two clamp primitives (both with an `unless errors present` guard)

- **Success-collapse:** exit 0 AND output matches a clean pattern with no error/warning →
  replace the WHOLE output with one line (`cmd: ok`, `no changes`, `up-to-date`).
- **Dedup-with-counts:** collapse runs of identical/near-identical lines to `line ×N`.

If ANY error/warning line exists, fall back to the signal-tiered caps instead of collapsing —
a collapse can NEVER hide a failure.

## tee cache — the failure escape hatch (folded from rtk)

Aggressive truncation is only safe if full context is recoverable WITHOUT re-running the
command (re-running re-burns tokens and may be non-deterministic). So:

- On any **non-zero exit**, OR whenever a cap clips a FAILING command, write the full
  unfiltered output to `.orchestrator/tee/<ts>_<cmd-slug>.log` and surface only the path:
  ```
  FAILED: 2/15 tests
  [full output: .orchestrator/tee/1707753600_npm_test.log]
  ```
- Config knob (in `.orchestrator/orient.toml`): `tee.mode = failures | always | never`
  (default `failures`). The agent reads the file lazily only if it needs more than the kept
  error lines.

This de-risks success-collapse: the bytes an agent needs on failure are never thrown away.

### CCR — make the clamp reversible

The tee file IS the cache; add a **stable handle + retrieve** so clamping is reversible, not
lossy. The handle is the tee path; surface a retrieve convention so a worker pulls the original on
demand instead of re-running the command:

```
retrieve <tee-path> [--lines a-b] [--grep PATTERN]
```

This turns "lossy by policy" into a "compress-cache-retrieve" decision point: clamp to a
summary/signature in context, keep the full original on disk keyed by the handle, fetch by handle
ONLY when the kept lines aren't enough — removing the main risk of aggressive clamping (losing the
one line that mattered) at zero up-front token cost. (We use a CCR (compress-cache-retrieve)
pattern and content-type routing — JSON/code/log/diff — but NOT a trained model or traffic proxy:
those contradict the terminal-first, zero-extra-process design.)

## Signatures-only reads (folded from rtk `read -l aggressive`)

When you need a file's API SURFACE (which functions/types/exports exist to call) — the common
case during intake and dependency scans — read it stripped to declarations with bodies elided.
A 600-line file collapses to ~40 lines of signatures. Detect language by extension; "minimal"
strips comments/blank lines, "aggressive" strips function bodies keeping only
signatures/declarations. ALWAYS fall back to raw content if stripping yields nothing. Use a
full-body read only when actually editing the body.

## Auto-rewrite hook (optional, guarantees adoption — folded from rtk `init -g`)

Where the host exposes a `PreToolUse`/pre-exec hook, bind `hooks/orient_rewrite.py`: it
transparently rewrites a bare shell call into its clamped form before execution
(`git status` → clamped, `<test>` → failures-only), so adoption is 100% across the main agent
AND every subagent at zero token overhead. An exclusion list in `.orchestrator/orient.toml`
keeps streaming/interactive/binary commands raw:

```toml
[hooks]
exclude_commands = ["curl", "wget", "playwright", "ssh", "docker run -it", "vim", "less"]
[tee]
mode = "failures"
```

Never rewrite an excluded command. Treat this config as untrusted, perception-shaping input
(see Safety below) — load it only after a human has reviewed and pinned its hash.

## Compound-command clamping (per-segment, pipe/redirect-safe)

Understand `&& || ; |`: (1) split on operators respecting quotes/escapes; (2) clamp each
segment via the catalog; (3) for a `|`, clamp ONLY the left producer, leave the pipe TARGET
raw (the consumer needs the unmodified stream); (4) never clamp a `find`/glob producer feeding
a pipe; (5) strip trailing redirects (`2>&1`, `>/dev/null`), clamp inner, re-append;
(6) unsplittable (`$(...)`, backticks, heredoc, file-target redirect) → run RAW with a tail
clamp, never corrupt.

## Density tiers by consumer

Route each artifact by WHO reads it: MACHINE tier (terse, fixed-schema) for worker→orchestrator
reports and internal digests; HUMAN tier (readable prose) for PR bodies and confirmations.
Skip a compression pass on already-dense content (code, config, lockfiles) — near-zero ratio,
real corruption risk.

## Fail-open (never a single point of failure)

Every reduction step is additive and removable. On ANY error, missing dependency, unparseable
payload, or unknown command, run the original command unchanged and propagate its REAL exit
status. A bad profile degrades to "slightly more tokens", never to "task dead".

## Safety overrides brevity (auto-clarity)

Compression YIELDS to safety. When a command/message is security-sensitive, irreversible
(force-push, history rewrite, prod deploy, data/schema delete, mass-file delete), or
order-dependent, FORCE full-clarity verbose output for that segment — the complete warning, the
exact command quoted verbatim, steps in explicit order — then resume terse mode. Optimization
may NEVER raise a command's risk tier. Treat any perception-shaping config (this skill's TOML,
clamp profiles, suppression lists) as untrusted until a human reviews and hash-pins it; silently
skip an untrusted or hash-changed version.

## Output

Run the command, return the clamped result (or the tee path on failure), and — when invoked
standalone — a one-line note of the recipe applied and tokens saved.
