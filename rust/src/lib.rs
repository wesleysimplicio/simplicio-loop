//! Rust/Tokio process supervisor backend, under the shared `ProcessSpec` / `ProcessLease` /
//! `ProcessResult` contract.
//!
//! ## Scope of this crate (issue #515, parent #498)
//!
//! #498 asks for a universal process supervisor: class-based queues, hierarchical quotas,
//! admission control, cgroups/Job-Object resource limits, fairness/aging, a Hub integration, and
//! more. #515 is the slice of that epic that puts a **native Rust/Tokio backend under the common
//! contract**. This crate implements exactly that slice:
//!
//! - [`spec::ProcessSpec`] — structured, argv-only spec (no shell strings), mirroring
//!   `simplicio_loop/process_supervisor.py::ProcessSpec` from #514 field-for-field.
//! - [`lease::ProcessLease`] — renewable/cancellable lease with the same state machine
//!   (`Active` → `Expired` | `Cancelled`) as the Python `ProcessLease`.
//! - [`result::ProcessResult`] — classified, bounded outcome with the same `error_code` taxonomy
//!   (`deadline_exceeded`, `cancelled`, `executable_not_found`, `spawn_error`).
//! - [`adapter::TokioProcessAdapter`] (Unix only in this pass) — spawns via
//!   `tokio::process::Command`, and on cancel/timeout kills the **whole process group**
//!   (`setsid` + `killpg`), not just the top PID, satisfying the AC "cancelar encerra
//!   descendentes e não deixa órfãos sem detecção" for the Unix case.
//!
//! **Explicitly out of scope for this pass** (left for follow-up #498 sub-issues): class-based
//! bounded queues, hierarchical quotas/admission control, cgroups v2 / Windows Job Object resource
//! limiting, fairness/aging, circuit breakers, the Hub RPC/wire integration, and the Windows side
//! of tree-kill (Job Objects). None of those are silently faked — they simply are not
//! implemented here yet.

pub mod lease;
pub mod result;
pub mod spec;

#[cfg(unix)]
pub mod adapter;

pub use lease::{LeaseState, ProcessLease};
pub use result::{ProcessResult, PROCESS_RESULT_SCHEMA};
pub use spec::{ProcessSpec, ProcessSpecBuilder, ProcessSpecError, PROCESS_SPEC_SCHEMA};

#[cfg(unix)]
pub use adapter::TokioProcessAdapter;
