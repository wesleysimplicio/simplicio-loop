# Host-neutral loop driver (`simplicio.loop-driver/v1`)

The stop hook and scheduler/self-paced tick are transports for one protocol.  A
driver must normalize each observation with `simplicio_loop.driver_contract` and
apply the same decision gates:

- `hook_missing`, `stop_requested`, and `iteration_cap` are `UNVERIFIED` blocks;
- a stop requires an exact promise plus measured watcher, evidence, and oracle gates;
- all other ticks continue with `gates_pending`;
- duplicate delivery of the same `event_id` is idempotent; a different payload is
  rejected as a conflicting duplicate.

`source` identifies the transport (`hook` or `self-paced`) only.  It is excluded
from duplicate comparison so a restart or host handoff cannot create a second
logical tick.
