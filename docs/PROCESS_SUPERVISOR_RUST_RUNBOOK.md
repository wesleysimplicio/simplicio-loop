# Rust/Tokio process-supervisor backend runbook (issue #515)

Parent epic: [#498](https://github.com/wesleysimplicio/simplicio-loop/issues/498). Companion to
[`docs/SUPERVISOR_ENFORCEMENT_RUNBOOK.md`](SUPERVISOR_ENFORCEMENT_RUNBOOK.md) (issue #516), whose
"Recommended next slice" section explicitly asked for this once #515's Rust backend landed. This
doc covers only the Rust backend integration: `simplicio_loop/process_supervisor_rust.py`
(`RustProcessAdapter`, `get_process_adapter`, `run_with_fallback`) and the
`rust/simplicio-supervisor/` crate it shells out to, wired into `HubDaemon.handle(method="execute")`
in `simplicio_loop/hub_daemon.py`.

## What it is

`RustProcessAdapter.run()` spawns the compiled `simplicio-supervisor` binary as a subprocess,
writes a JSON `ProcessSpec` payload to its stdin, and parses a JSON `ProcessResult` from its
stdout (`simplicio_loop/process_supervisor_rust.py:85-113`). `rust/simplicio-supervisor/src/main.rs`
reads stdin, deserializes it with `serde_json`, calls `run()` (spawn + deadline + process-group
kill via `killpg`), and serializes the result back to stdout. `get_process_adapter()` picks this
adapter when `rust_binary_path()` finds a built binary (`target/release` or `target/debug` under
`rust/simplicio-supervisor/`, falling back to `$PATH` via `shutil.which`); otherwise it silently
returns `PythonProcessAdapter` — there is no persistent state or caching, so this choice is
re-evaluated on every call.

## Failure modes (verified against the current code, not hypothetical)

1. **Binary not built.** `rust_binary_path()` returns `None`, `get_process_adapter()` returns
   `PythonProcessAdapter`, and `backend_name()` reports `"python-fallback"`. This is the
   designed, safe default — confirmed by
   `test_get_process_adapter_falls_back_to_python_when_binary_absent`. No operator action needed;
   this is not degradation, it is the intended state when the crate hasn't been built.

2. **Malformed/empty stdout from the binary escapes `HubDaemon`'s error handling.** If the
   compiled binary exits early with `std::process::exit(2)` (invalid `ProcessSpec` JSON per
   `rust/simplicio-supervisor/src/main.rs:9-17`, or a stdin read failure), stdout is empty.
   `RustProcessAdapter.run()`'s `json.loads(stdout)` (`process_supervisor_rust.py:113`) then
   raises `json.JSONDecodeError`, a `ValueError` subclass. `HubDaemon.handle()`'s
   `except (OSError, RuntimeError, asyncio.CancelledError)` clause
   (`simplicio_loop/hub_daemon.py:238`) does **not** catch `ValueError`, and
   `HubDaemon._dispatch()` (`hub_daemon.py:449-453`) only catches `HubError` — so this exception
   propagates unhandled out of `handle()`/`_dispatch()` instead of returning a structured
   `{"ok": false, "error": ...}` response. In practice this needs a spec/binary schema mismatch
   (e.g. a new `ProcessSpec` field the Rust struct doesn't know) to trigger; today's Python↔Rust
   field set is aligned so it is latent, not observed, but it is a real gap in the exception
   surface as written.

3. **The binary hangs past its deadline.** `process.communicate(..., timeout=timeout_seconds)`
   raises `subprocess.TimeoutExpired` (`process_supervisor_rust.py:109`), which is re-raised
   after killing the process. `subprocess.TimeoutExpired` is a `SubprocessError`, not an
   `OSError`/`RuntimeError` — it has the same gap as failure mode 2: `HubDaemon.handle()`'s except
   clause does not catch it, so it also propagates unhandled rather than becoming a clean Hub
   error response. Covered by `test_rust_adapter_run_kills_on_timeout`, which proves the process
   is actually killed (the exception still surfaces, only the "clean error response" part is the
   gap).

4. **`on_spawned` callback raises.** Deliberately swallowed
   (`process_supervisor_rust.py:100-104`, `try/except Exception: pass`) so a registry-bookkeeping
   failure (e.g. `ProcessRegistry.register` I/O error) never blocks or fails the actual process
   run. Covered by `test_rust_adapter_run_swallows_on_spawned_exception`. This is by design, not a
   bug — but it means registry bookkeeping failures are silent; they will not show up as a Hub
   error, only as a stale/missing entry in `ProcessRegistry`.

5. **Resolved:** a second, orphaned Rust crate (`rust/` top-level `Cargo.toml` + `src/`) used to
   sit alongside `rust/simplicio-supervisor/` — flagged in the issue's comment thread (2026-07-18)
   as unreferenced duplication (`grep` confirmed nothing in the Python codebase imports
   `rust/src/*` or `rust/Cargo.toml`). It has been deleted; `rust/simplicio-supervisor/` is now the
   only Rust crate in this repo, so there is no risk of the two drifting out of sync.

## Detecting degradation

- **Per-call:** every successful `HubDaemon.handle(method="execute")` response includes
  `"backend"` — `"rust"` or `"python-fallback"` (`hub_daemon.py:243`). A fleet that expects the
  Rust backend but keeps seeing `"python-fallback"` means the binary isn't built/on `$PATH` on
  that host.
- **Programmatically:** `simplicio_loop.process_supervisor_rust.rust_backend_available()` (bool)
  or `backend_name()` (`"rust"`/`"python-fallback"`) can be called directly, no daemon required.
- **Unhandled-exception cases (failure modes 2 and 3 above):** these do not produce a
  `{"ok": false}` Hub response at all — the client sees the connection drop/EOF instead of a JSON
  error envelope. Check the Hub process's own stderr/logs for a `JSONDecodeError` or
  `TimeoutExpired` traceback originating in `process_supervisor_rust.py` to distinguish this from
  an ordinary `HubError`.

## Rollback

There is no feature flag for this backend today — rollback is physical, not configuration:

1. **Fastest:** delete or rename the compiled binary(ies):
   `rust/simplicio-supervisor/target/release/simplicio-supervisor` and
   `rust/simplicio-supervisor/target/debug/simplicio-supervisor`. `rust_binary_path()` re-checks
   these paths on every call (no caching), so the very next `execute` call falls back to
   `PythonProcessAdapter` — no daemon restart required.
2. If a `simplicio-supervisor` binary is also installed on `$PATH` (the `shutil.which` fallback,
   `process_supervisor_rust.py:27-28`), remove it from `$PATH` too, or the fallback lookup will
   still find it after step 1.
3. **Longer-term:** simply don't run `cargo build` for `rust/simplicio-supervisor/` in that
   environment — the Python side has no build-time dependency on the crate; `PythonProcessAdapter`
   is a complete, independently-tested substitute (`tests/test_process_supervisor_spec.py`,
   `tests/test_process_supervisor_hardening.py`).

## Test coverage measured for this slice

```bash
python3 -m coverage run -m pytest -q tests/test_process_supervisor_rust.py tests/test_hub_supervisor_epic_e2e.py
python3 -m coverage report -m --include='simplicio_loop/process_supervisor_rust.py'
```

`simplicio_loop/process_supervisor_rust.py`: **100% line coverage** (60/60 statements) measured
2026-07-18, exercising both the fallback path, the shutil.which lookup, the `RuntimeError` when no
binary is configured, the swallowed `on_spawned` exception, the `TimeoutExpired` kill path (via a
stub executable standing in for the real binary, so this is exercised even when the Rust crate
isn't built), and — when the crate *is* built — a real end-to-end run through the compiled binary
via `HubDaemon`.

Rust side: `cargo test` in `rust/simplicio-supervisor/` — 21/21 passing (19 unit tests in
`src/lib.rs` + 2 binary end-to-end tests in `tests/cli.rs` that spawn the compiled
`simplicio-supervisor` binary and feed it stdin, the same way `process_supervisor_rust.py` does).

`cargo tarpaulin`'s default ptrace engine measured **82.95% (107/129 lines)** — up from the
previous 62.99%, but `src/main.rs` still reported 0/15 credited lines even with `tests/cli.rs`
added, because ptrace traces only the test binary itself; `tests/cli.rs` spawns
`simplicio-supervisor` as a genuinely separate process (a new `exec`, not a fork the tracer
follows), so its instructions are invisible to that engine regardless of how it's invoked.

`cargo tarpaulin --engine llvm` (source-based coverage, propagates via `LLVM_PROFILE_FILE` across
subprocess boundaries) measured **94.74% line coverage (126/133 lines)**: `src/lib.rs` at 113/118
and `src/main.rs` at 13/15 — this **is** above the issue's 85% target. Both numbers are honestly
measured, not rounded; report whichever engine's number you use, since they diverge only because
of a coverage-tool limitation (ptrace's process-boundary blindness), not a difference in what's
actually tested. Remaining uncovered lines under the `llvm` engine: the body of `libc_setsid()`
and the `pre_exec` closure that calls it (`src/lib.rs:161-162,246,248`) — this closure runs
*inside the forked child, before `exec`*, so parent-process coverage instrumentation cannot
observe it even though the behavior is exercised by every Unix test that spawns a child; the
`child.id() == None` branch of `kill_tree` (`src/lib.rs:263`, an edge case that would need a child
that dies between `spawn()` and `kill_tree()` being called); and `main.rs`'s stdin-read-failure
branch (`src/main.rs:9-10`, hard to trigger portably without simulating a stdin I/O error).

Reproduce with: `cargo test && cargo tarpaulin --engine llvm --out Stdout` from
`rust/simplicio-supervisor/`.
