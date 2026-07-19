# Verified Agent Delivery

`VerifiedAgentDelivery` is the Loop→Runtime→Execution Board protocol for one agent attempt.
The Loop emits a validated `simplicio.loop-event/v1` transition, the negotiated Runtime
accepts or durably buffers that event, and the Execution Board projects the same transition
into its event-sourced card. The board is therefore a read model, not a status field that an
agent can forge.

## Delivery gates

1. `LoopRuntimeAdapter.negotiate()` must establish `simplicio.runtime/v1`.
2. Every transition is validated by the phase state machine and carries run/work-item and
   attempt identity.
3. Evidence must be a fresh `COMPLETE` receipt; a `PASS` without `ready: true` is rejected.
4. The watcher must independently report `match: true` with a non-empty challenge.
5. Delivery convergence must be recorded explicitly before `complete()`. A local fixture must
   say `target="local-fixture"` and remains local proof only; a real merge queue must carry
   acceptance evidence (for example a measured merge-queue receipt SHA + accepted status).
6. Only then may `complete()` emit `done`; Runtime outage remains `BUFFERED` and never claims
   delivery.

```python
from simplicio_loop.verified_delivery import VerifiedAgentDelivery

delivery = VerifiedAgentDelivery(runtime=runtime, board=board, attempt_id="attempt-1")
for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
    delivery.transition(phase)
delivery.record_evidence(receipt)
delivery.record_watcher(match=True, challenge="replay run-1")
delivery.record_delivery({"target": "local-fixture", "satisfied": True})
delivery.complete(receipt)
```

The local board receipt is measured evidence of the projection only. An external board must be
explicitly bound; absence is reported as `UNVERIFIED`, never as an external delivery pass.
Likewise, `completion_percent=100` on the Execution Board only happens when every card is `done`
and every card has recorded delivery convergence. Local fixture convergence is tracked separately
from real merge-queue convergence so E2E fixtures do not masquerade as a live merge queue.
