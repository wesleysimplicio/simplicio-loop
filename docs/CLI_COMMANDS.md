# CLI command reference

Every installed entry point and every public subcommand accepts `--help`.
Use the most specific form, such as `simplicio-loop queue top --help` or
`simplicio-process-supervisor reports --help`.

## Installed entry points

| Entry point | Purpose |
|---|---|
| `simplicio-loop` | Main orchestrator: plan, execute, verify, deliver, learn, and release-train work. |
| `issue-factory` | Discover ready work items from a configured source adapter. |
| `simplicio-ecosystem-doctor` | Inspect installed operator versions, capabilities, and route readiness. |
| `simplicio-loop-tools` | Run the consumer/tooling surface for Loop artifacts. |
| `simplicio-hub` | Start or inspect the local Hub daemon (`serve`, `doctor`). |
| `simplicio-remote-queue-server` | Serve the remote task queue. |
| `simplicio-remote-worker` | Claim, enqueue, cancel, or serve remote work. |
| `simplicio-remote-worker-supervisor` | Supervise bounded remote worker processes. |
| `simplicio-process-supervisor` | Inspect and control supervised processes (`status`, `top`, `queue`, `cancel`, `drain`, `reports`). |

## `simplicio-loop` commands

| Command | Purpose |
|---|---|
| `install` | Install bundled skills and hooks into a supported runtime. |
| `dashboard` | Open or stop the token-monitor dashboard. |
| `task` | Compile, validate, or preview a Markdown task contract. |
| `prototype` | Route prototype planning and validation commands. |
| `plan` | Compile a raw task into a frozen contract. |
| `run` | Arm, execute, and independently verify a task. |
| `orient` | Build bounded context through Fast with Mapper fallback. |
| `retrieve` | Retrieve and verify a tee-cache result. |
| `extensions doctor` | Inspect an exact extension-provider/runtime handshake. |
| `oracle` | Evaluate completion and cross-runtime parity. |
| `status` | Inspect the latest or a selected run. |
| `stack lock/verify` | Create or verify an installed-stack lock. |
| `doctor` | Inspect stack identity, source adapters, resources, or storage routing. |
| `inspect` | Inspect MapperStore capabilities and storage routing. |
| `map` | Inspect or build map-service receipts. |
| `preflight` | Verify Mapper, Dev CLI, Runtime, and Fast operators. |
| `deploy` | Plan a gated deployment; `--apply` is explicit. |
| `verify` | Run independent watcher and delivery gates. |
| `progress` | Render run progress as text, JSON, Markdown, or ANSI. |
| `resume` | Resume a non-terminal run. |
| `tick` | Execute one planned task through Dev CLI. |
| `batch` | Dispatch ready tasks with bounded isolated workers. |
| `cancel` | Cancel a non-terminal run. |
| `checkpoint` | Inspect, cancel, or garbage-collect Fast V3 checkpoints. |
| `maintenance-deferred` | Record a maintenance-deferred backlog transition. |
| `deliver` | Reconcile delivery state with source evidence. |
| `decide` | Apply a human decision and invalidate dependent artifacts. |
| `sync-source` | Requery external source state and reconcile delivery. |
| `drain` | Evaluate or persist a queue-drain receipt. |
| `agent-slots` | Inspect and reclaim Loop-owned agent capacity. |
| `generation-broker` | Inspect and reconcile persisted generation bindings. |
| `queue` | Operate the durable queue (`status`, `top`, `drain`, `resume`, `doctor`, `reclaim`, `gc`, `migrate`, `inspect`, `cancel`). |
| `single-task-fast` | Select the bounded single-task local-first route. |
| `ledger` | Replay or validate the operational event ledger. |
| `findings` | List, report, reconcile, diagnose, or import routed findings. |
| `learn retrospective` | Derive durable lessons from completed runs. |
| `release-train check` | Validate ecosystem release schemas and local drift. |
| `hub-drain-plan` | Read-only GitHub drain intake. |
| `hub-drain-admit` | Admit a held final checkpoint without dispatching it. |

## Offline journal replay

`python scripts/journal_replay.py <suite.json> --check` replays committed
`simplicio.journal-replay-suite/v1` fixtures through the production journal and recovery
modules without network access. It emits a canonical
`simplicio.journal-replay-receipt/v1` JSON receipt and exits non-zero when an observed
outcome differs from `expected_outcome`.

## Operator order for LLMs

1. `simplicio-mapper --help` → `scan` → `inspect` → `handoff`.
2. `simplicio-fast --help` when Fast is operational; it supplies bounded context, not authority.
3. `simplicio-dev-cli --help` → `task --help` for the governed edit and verification step.
4. `simplicio-loop preflight --help`, focused tests, then `simplicio-loop verify --help`.

The current coordinated train is Mapper `0.26.10`, Dev CLI `0.18.6`, Fast
`2.0.22`, and Loop `3.38.30`. When a command is added, add a meaningful
`help=` string, document it here, and add a `--help` regression check.
