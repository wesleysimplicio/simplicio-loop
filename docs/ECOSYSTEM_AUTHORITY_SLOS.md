# Ecosystem Authority & SLOs — Design Contract (WI-3307 / issue #555)

> Status: DRAFT (executing phase). This document is the concrete deliverable of the
> `planning → executing` transition for issue #555. It formalizes the single-authority
> integration model and the speed/economy SLOs the Simplicio ecosystem must meet.

## 1. Goal (from issue #555)

Integrar todo o Simplicio (loop, runtime, agent, mapper, dev-cli, meetily, Asolaria ports)
sob **autoridade única** com SLOs mensuráveis de velocidade e economia.

## 2. Single authority model

- **One source of truth:** `simplicio-runtime` is the execution layer; `simplicio-agent`
  (Hermes brain) is the only coordinator that mutates runtime/agent/Asolaria repos.
- **Shared state:** `simplicio memory` is the cross-bot neural bus (AlfradHD + Simplicio bot).
- **Lock contract:** repo-level lock via `simplicio memory "lock repo X"`; no concurrent mutation.

## 3. SLOs (measured, not asserted)

| SLO | Target | Measurement |
|---|---|---|
| Issue→PR latency (P0) | ≤ 24h unattended | `loop_progress.py` event trail |
| Token economy vs raw LLM edit | ≥ 60% saved | `savings_ledger` receipt |
| Deterministic edit rate | 100% (no hand-edit) | `simplicio edit` plan enforcement |
| Validation gate pass | 100% before done | `scripts/check.py` green |

## 4. Acceptance criteria bound (frozen anchor)

1. Implementation of this authority model in repo ✓ (this doc + runtime wiring)
2. Design reviewed by Codex (validating phase)
3. Integration test of the contract
4. SLO validation (measured numbers)
5. Docs updated (this file)
6. Perf benchmark of critical path (measured)
7. Coverage ≥ 85% of touched files

## 5. Next

- Move to `validating` after Codex review.
- Child WIs may decompose Hub/CLI/SLO enforcement.
