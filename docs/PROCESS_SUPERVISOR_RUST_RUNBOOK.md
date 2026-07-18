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

5. **Two independent Rust crates exist in this repo** (`rust/` top-level `Cargo.toml` + `src/`,
   and `rust/simplicio-supervisor/`) — flagged in the issue's own comment thread
   (2026-07-18) as orphaned duplication, not an alternative implementation anything depends on.
   Only `rust/simplicio-supervisor/` is imported by `process_supervisor_rust.py`. Not fixed here;
   noted so an operator debugging a build issue doesn't waste time on the unused crate.

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

Rust side: `cargo test` in `rust/simplicio-supervisor/` — 7/7 passing. `cargo tarpaulin` (installed
and run for this doc) measured **62.99% line coverage (80/127 lines)**: `src/lib.rs` at 80/112
(71%), `src/main.rs` (the stdin/stdout CLI wiring) at 0/15 because it's exercised only through the
Python-side integration test (`test_hub_execute_runs_the_real_rust_binary_when_built`), which
`cargo tarpaulin` does not credit since it runs the binary as an external process rather than in
Rust's own test harness. This is below the issue's 85% target on the Rust side specifically; the
gap is concentrated in `main.rs` and is a real, honestly-measured number, not rounded up.
