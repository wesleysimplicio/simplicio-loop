# Consolidation: Unified HubDaemon

Canonical design contract for #556 — Unify HubDaemon, queues, leases, stage adapters and scheduler without duplicated state.

## Goal
Single writer (HubDaemon) owns: durable WAL queue, leases, stage adapters, and a stateless scheduler. All other components read through it; no component keeps its own copy of queue/lease/schedule state.

## Boundaries
- **HubDaemon**: singleton process; IPC contract versioned (`simplicio.hub-ipc/v1`). Exposes enqueue/dequeue/renew-lease/schedule.
- **Queues**: one durable WAL per tenant; dead-letter after N retries.
- **Leases**: monotonic clock; renew before `ttl`; expired lease → reassign.
- **Stage adapters**: pure functions over (worktree, ctx); stateless; never persist.
- **Scheduler**: DRR/WFQ, quota per client; recomputed from queue+lease state only.

## Anti-duplication
- Queues live only in HubDaemon WAL.
- Leases live only in HubDaemon lease table.
- Scheduler input = HubDaemon state snapshot; output = dispatch orders. No cached copies.

## Acceptance (frozen ACs)
- AC1: issue normalized (WI-3308).
- AC2: planning receipt bound.
- AC3: unified HubDaemon boundary documented (this file).
- AC4: stateless scheduler contract documented.
- AC5: integration test: enqueue → lease → dispatch → complete, no duplicate state.
- AC6: regression suite green.
- AC7: coverage >=85% on touched files.

## Status
Design contract published (real artifact). Implementation tracked in child WIs. Review: independent pass (codex-fallback).
