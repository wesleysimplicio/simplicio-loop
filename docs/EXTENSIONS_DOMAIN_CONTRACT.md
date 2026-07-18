# Domain Extension Contract for loop-oss & loop-marketing (WI-3309 / issue #557)

> Status: DRAFT (executing phase). Concrete deliverable of `planning → executing` for #557.

## 1. Goal (from issue #557)

Formalizar o contrato de extensões de domínio para `loop-oss` e `loop-marketing`.

## 2. Extension point shape

Every domain extension MUST implement:

```python
class DomainExtension:
    domain: str                 # "oss" | "marketing"
    def intake(self, issue) -> WorkItem: ...
    def plan(self, wi) -> PlanReceipt: ...
    def execute(self, wi, ctx) -> Result: ...
```

## 3. Contract rules

- `intake` returns a normalized WorkItem with canonical_state=`intake`.
- `plan` MUST bind acceptance criteria + planning receipt before mutation (fail-closed).
- `execute` runs only inside an isolated worktree; no main mutation.
- Transitions are locked to: intake→mapping→planning→executing→validating→watching→delivering→done.

## 4. loop-oss vs loop-marketing specifics

| Domain | Source | Extra AC |
|---|---|---|
| oss | GitHub issues | PR merge = delivery receipt |
| marketing | campaign board | published asset = delivery receipt |

## 5. Acceptance criteria bound (frozen anchor)

1. Contract implemented (this doc + schema)
2. Codex review of the extension boundary
3. Integration test: oss + marketing extensions load
4. Validation that both satisfy the transition lock
5. Docs updated
6. Perf benchmark: extension load time (measured)
7. Coverage ≥ 85% of extension loader

## 6. Next

- `validating` after Codex review. Child WIs: oss adapter, marketing adapter.
