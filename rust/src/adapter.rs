//! Tokio-based spawn + cancel implementation of the process contract (Unix only for now).
//!
//! Cross-platform note (#515 AC: "cancelar encerra descendentes e não deixa órfãos"): the
//! process is started via `setsid()` in a `pre_exec` hook, making it both its own session leader
//! and process-group leader. Cancelling or timing out therefore sends `SIGKILL` to the whole
//! **process group** (`killpg`), not just the top PID — a shell chain that forks children (e.g.
//! `sh -c 'sleep 30 & wait'`) is torn down in full, not left as an orphan reparented to init.
//! `tests/spawn_cancel.rs` proves this by spawning such a chain, recording the descendant's PID to
//! a file, cancelling the lease, and asserting the descendant is gone.
//!
//! Windows is **not implemented in this pass** — the equivalent primitive there is a Job Object
//! (`CreateJobObject` + `AssignProcessToJobObject` + `TerminateJobObject`, mirroring what MSDN and
//! most process-tree-aware supervisors use instead of `TerminateProcess` on just the top PID).
//! That is tracked as explicit follow-up work for #515/#498, not silently skipped.

use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::os::unix::process::CommandExt;
use std::process::Stdio;
use std::time::Instant;

use tokio::io::AsyncReadExt;
use tokio::process::Command as TokioCommand;
use tokio::sync::oneshot;
use tokio::time::Duration as TokioDuration;

use crate::result::ProcessResult;
use crate::spec::ProcessSpec;

/// Rust/Tokio backend for the process contract. Mirrors
/// `simplicio_loop.process_supervisor.PythonProcessAdapter`.
#[derive(Debug, Default, Clone, Copy)]
pub struct TokioProcessAdapter;

enum RunOutcome {
    Exited(std::io::Result<std::process::ExitStatus>),
    TimedOut,
    Cancelled,
}

impl TokioProcessAdapter {
    pub fn new() -> Self {
        Self
    }

    /// Allowlisted parent-env passthrough, overridden by `spec.env` — mirrors
    /// `PythonProcessAdapter._environment`.
    fn environment(spec: &ProcessSpec) -> Vec<(String, String)> {
        let mut merged: BTreeMap<String, String> = BTreeMap::new();
        for key in &spec.env_allowlist {
            if let Ok(value) = std::env::var(key) {
                merged.insert(key.clone(), value);
            }
        }
        for (key, value) in &spec.env {
            merged.insert(key.clone(), value.clone());
        }
        merged.into_iter().collect()
    }

    fn bounded(raw: Vec<u8>, limit: usize) -> (String, bool) {
        let truncated = raw.len() > limit;
        let slice = if truncated { &raw[..limit] } else { &raw[..] };
        (String::from_utf8_lossy(slice).into_owned(), truncated)
    }

    fn kill_tree(pgid: Option<i32>) {
        if let Some(pgid) = pgid {
            // SAFETY: `killpg` only signals a process group we own (the child we just spawned
            // with `setsid()`, whose pid equals its own pgid); no pointers are dereferenced.
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
    }

    /// Spawn `spec`, run it to completion, timeout, or external cancellation (via `cancel`), and
    /// return a classified [`ProcessResult`]. On timeout/cancel the **entire process group** is
    /// killed, not just the top PID.
    pub async fn run(
        &self,
        spec: &ProcessSpec,
        lease_id: &str,
        cancel: oneshot::Receiver<()>,
    ) -> ProcessResult {
        let started = Instant::now();

        let mut command = std::process::Command::new(&spec.argv[0]);
        command.args(&spec.argv[1..]);
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        command.env_clear();
        for (key, value) in Self::environment(spec) {
            command.env(key, value);
        }
        command.stdin(Stdio::null());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        // SAFETY: the closure only calls the async-signal-safe `setsid(2)` between fork and
        // exec, and does no allocation/locking — the standard precondition for `pre_exec`.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }

        let mut tokio_command = TokioCommand::from(command);
        tokio_command.kill_on_drop(true);

        let mut child = match tokio_command.spawn() {
            Ok(child) => child,
            Err(err) => {
                let error_code = if err.kind() == ErrorKind::NotFound {
                    "executable_not_found"
                } else {
                    "spawn_error"
                };
                return ProcessResult {
                    duration_seconds: started.elapsed().as_secs_f64(),
                    error_code: error_code.to_string(),
                    lease_id: lease_id.to_string(),
                    ..ProcessResult::default()
                };
            }
        };

        // The child called setsid(): its pid is also its own session id AND process group id.
        let pgid = child.id().map(|pid| pid as i32);

        let stdout = child.stdout.take().expect("stdout was piped");
        let stderr = child.stderr.take().expect("stderr was piped");
        let stdout_task = tokio::spawn(async move {
            let mut buf = Vec::new();
            let mut reader = stdout;
            let _ = reader.read_to_end(&mut buf).await;
            buf
        });
        let stderr_task = tokio::spawn(async move {
            let mut buf = Vec::new();
            let mut reader = stderr;
            let _ = reader.read_to_end(&mut buf).await;
            buf
        });

        let mut cancel = cancel;
        let outcome = if let Some(timeout_secs) = spec.timeout_seconds {
            tokio::select! {
                status = child.wait() => RunOutcome::Exited(status),
                _ = tokio::time::sleep(TokioDuration::from_secs_f64(timeout_secs)) => RunOutcome::TimedOut,
                _ = &mut cancel => RunOutcome::Cancelled,
            }
        } else {
            tokio::select! {
                status = child.wait() => RunOutcome::Exited(status),
                _ = &mut cancel => RunOutcome::Cancelled,
            }
        };

        let (returncode, timed_out, cancelled, error_code) = match outcome {
            RunOutcome::Exited(Ok(status)) => (status.code(), false, false, String::new()),
            RunOutcome::Exited(Err(_)) => (None, false, false, "wait_error".to_string()),
            RunOutcome::TimedOut => {
                Self::kill_tree(pgid);
                let status = child.wait().await.ok();
                (
                    status.and_then(|s| s.code()),
                    true,
                    false,
                    "deadline_exceeded".to_string(),
                )
            }
            RunOutcome::Cancelled => {
                Self::kill_tree(pgid);
                let status = child.wait().await.ok();
                (
                    status.and_then(|s| s.code()),
                    false,
                    true,
                    "cancelled".to_string(),
                )
            }
        };

        let stdout_bytes = stdout_task.await.unwrap_or_default();
        let stderr_bytes = stderr_task.await.unwrap_or_default();
        let (stdout, stdout_truncated) = Self::bounded(stdout_bytes, spec.max_output_bytes);
        let (stderr, stderr_truncated) = Self::bounded(stderr_bytes, spec.max_output_bytes);

        ProcessResult {
            returncode,
            stdout,
            stderr,
            duration_seconds: started.elapsed().as_secs_f64(),
            timed_out,
            cancelled,
            truncated: stdout_truncated || stderr_truncated,
            error_code,
            lease_id: lease_id.to_string(),
        }
    }
}
