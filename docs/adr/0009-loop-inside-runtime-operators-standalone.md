# ADR 0009: Loop complete inside Runtime; mapper / dev-cli / fast work alone

- **Status:** Accepted
- **Date:** 2026-08-04
- **Mirrors:** `simplicio-runtime` ADR-2026-08-04 + ADR-2026-08-04b

## Context

Product law: the full loop (convergence, journals, activation, completion) lives
**inside Runtime**, and Runtime decides when to use it. Simultaneously, the
operator trio must remain usable without Runtime or the loop becomes a single
point of failure for ordinary survey/edit work.

## Decision

1. **Loop product path** is Runtime-owned. Prefer `runtime-backed`. Activation
   via `simplicio loop decide` (or Runtime spine). Hosts do not bypass Runtime
   when it is available.
2. **Operators are standalone-capable:**
   - `simplicio-mapper` — map / inspect / handoff without Runtime
   - `simplicio-dev-cli` — plan + deterministic edits without Runtime
   - `simplicio-fast` — understand / plan / apply / mmap when installed, without Runtime
3. When Runtime is missing: operators continue; report
   `UNVERIFIED|runtime_unavailable`; do not claim full runtime-backed loop
   completion.
4. This package remains the protocol + host-hook implementation under Runtime
   authority — not a peer control plane.

## Consequences

Host rules MUST #0 (Runtime owns loop) and MUST operator survey/mutate paths
remain valid without Runtime. See also ADR 0010 (execution metrics standard).
