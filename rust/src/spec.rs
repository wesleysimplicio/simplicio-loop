//! Structured, argv-only process specification.
//!
//! Mirrors `simplicio_loop/process_supervisor.py::ProcessSpec` (issue #514) field-for-field so a
//! `ProcessSpec` decided in Python and one built in Rust describe the same contract. Shell
//! execution is forbidden by construction: callers must pass a structured `argv`, never a shell
//! string, exactly like the Python adapter's `shell=False` invariant.

use std::collections::BTreeMap;
use std::fmt;
use std::path::Path;

use sha2::{Digest, Sha256};

pub const PROCESS_SPEC_SCHEMA: &str = "simplicio.process-spec/v1";

/// Raised when a [`ProcessSpec`] would be unsafe or incomplete — the Rust analogue of the
/// Python contract's `ProcessSpecError(ValueError)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessSpecError(pub String);

impl ProcessSpecError {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for ProcessSpecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ProcessSpecError {}

/// Structured, argv-only process contract. See `simplicio_loop/process_supervisor.py`.
#[derive(Debug, Clone, PartialEq)]
pub struct ProcessSpec {
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: BTreeMap<String, String>,
    pub env_allowlist: Vec<String>,
    /// `None` means "no deadline" (mirrors Python's `timeout_seconds=None`).
    pub timeout_seconds: Option<f64>,
    pub max_output_bytes: usize,
    pub priority: u32,
    pub idempotency_key: String,
    /// Always `false`. Kept as a field (rather than omitted) so `to_dict`/serialization keeps
    /// the same shape as the Python contract; constructing a spec with `shell = true` is
    /// rejected below, exactly like `ProcessSpec.__post_init__` on the Python side.
    pub shell: bool,
}

/// Builder mirroring the defaults of the Python `dataclass`.
#[derive(Debug, Clone)]
pub struct ProcessSpecBuilder {
    argv: Vec<String>,
    cwd: Option<String>,
    env: BTreeMap<String, String>,
    env_allowlist: Vec<String>,
    timeout_seconds: Option<f64>,
    max_output_bytes: usize,
    priority: u32,
    idempotency_key: String,
    shell: bool,
}

impl ProcessSpecBuilder {
    pub fn new<I, S>(argv: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            argv: argv.into_iter().map(Into::into).collect(),
            cwd: None,
            env: BTreeMap::new(),
            env_allowlist: Vec::new(),
            timeout_seconds: Some(30.0),
            max_output_bytes: 65_536,
            priority: 0,
            idempotency_key: String::new(),
            shell: false,
        }
    }

    pub fn cwd(mut self, cwd: impl Into<String>) -> Self {
        self.cwd = Some(cwd.into());
        self
    }

    pub fn env(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.insert(key.into(), value.into());
        self
    }

    pub fn env_allowlist<I, S>(mut self, keys: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.env_allowlist = keys.into_iter().map(Into::into).collect();
        self
    }

    pub fn timeout_seconds(mut self, timeout: Option<f64>) -> Self {
        self.timeout_seconds = timeout;
        self
    }

    pub fn max_output_bytes(mut self, bytes: usize) -> Self {
        self.max_output_bytes = bytes;
        self
    }

    pub fn priority(mut self, priority: u32) -> Self {
        self.priority = priority;
        self
    }

    pub fn idempotency_key(mut self, key: impl Into<String>) -> Self {
        self.idempotency_key = key.into();
        self
    }

    /// Present so callers can prove the forbidden path is rejected; there is no way to build a
    /// spec with `shell = true` that survives `build()`.
    pub fn shell(mut self, shell: bool) -> Self {
        self.shell = shell;
        self
    }

    pub fn build(self) -> Result<ProcessSpec, ProcessSpecError> {
        if self.argv.is_empty() || self.argv.iter().any(|value| value.is_empty()) {
            return Err(ProcessSpecError::new(
                "argv must contain a non-empty executable",
            ));
        }
        if self.shell {
            return Err(ProcessSpecError::new("shell execution is forbidden"));
        }
        if let Some(cwd) = &self.cwd {
            if !Path::new(cwd).is_absolute() {
                return Err(ProcessSpecError::new("cwd must be absolute"));
            }
        }
        if let Some(timeout) = self.timeout_seconds {
            if timeout <= 0.0 {
                return Err(ProcessSpecError::new("timeout_seconds must be positive"));
            }
        }
        if self.max_output_bytes < 1 {
            return Err(ProcessSpecError::new("max_output_bytes must be positive"));
        }

        let mut allowlist: Vec<String> = self.env_allowlist.into_iter().collect();
        allowlist.sort();
        allowlist.dedup();
        if let Some(missing) = self
            .env
            .keys()
            .find(|key| !allowlist.iter().any(|allowed| allowed == *key))
        {
            let _ = missing;
            return Err(ProcessSpecError::new(
                "env contains a key outside env_allowlist",
            ));
        }

        Ok(ProcessSpec {
            argv: self.argv,
            cwd: self.cwd,
            env: self.env,
            env_allowlist: allowlist,
            timeout_seconds: self.timeout_seconds,
            max_output_bytes: self.max_output_bytes,
            priority: self.priority,
            idempotency_key: self.idempotency_key,
            shell: false,
        })
    }
}

impl ProcessSpec {
    pub fn builder<I, S>(argv: I) -> ProcessSpecBuilder
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        ProcessSpecBuilder::new(argv)
    }

    /// Canonical JSON representation, matching the field names/shape of
    /// `ProcessSpec.to_dict()` on the Python side.
    fn canonical_json(&self) -> String {
        // `serde_json::Map` is backed by a `BTreeMap` (the `preserve_order` feature is not
        // enabled), so object keys are emitted sorted — the same effect as Python's
        // `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
        let value = serde_json::json!({
            "argv": self.argv,
            "cwd": self.cwd,
            "env": self.env,
            "env_allowlist": self.env_allowlist,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "priority": self.priority,
            "idempotency_key": self.idempotency_key,
        });
        serde_json::to_string(&value).expect("spec fields are always serializable")
    }

    /// Deterministic content hash of the spec, mirroring `ProcessSpec.spec_hash`.
    pub fn spec_hash(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.canonical_json().as_bytes());
        let digest = hasher.finalize();
        digest.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    /// Full contract dict (schema + spec_hash), matching `ProcessSpec.to_dict()`.
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "schema": PROCESS_SPEC_SCHEMA,
            "argv": self.argv,
            "cwd": self.cwd,
            "env": self.env,
            "env_allowlist": self.env_allowlist,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "priority": self.priority,
            "idempotency_key": self.idempotency_key,
            "shell": false,
            "spec_hash": self.spec_hash(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_shell_execution() {
        let err = ProcessSpec::builder(["echo"])
            .shell(true)
            .build()
            .unwrap_err();
        assert_eq!(err.0, "shell execution is forbidden");
    }

    #[test]
    fn rejects_env_outside_allowlist() {
        let err = ProcessSpec::builder(["echo"])
            .env("NO", "x")
            .build()
            .unwrap_err();
        assert_eq!(err.0, "env contains a key outside env_allowlist");
    }

    #[test]
    fn rejects_empty_argv() {
        let err = ProcessSpec::builder(Vec::<String>::new())
            .build()
            .unwrap_err();
        assert_eq!(err.0, "argv must contain a non-empty executable");
    }

    #[test]
    fn rejects_relative_cwd() {
        let err = ProcessSpec::builder(["echo"])
            .cwd("relative/path")
            .build()
            .unwrap_err();
        assert_eq!(err.0, "cwd must be absolute");
    }

    #[test]
    fn accepts_allowlisted_env_and_reports_shell_false() {
        let spec = ProcessSpec::builder(["echo", "hi"])
            .env("SIMPLICIO_TEST", "yes")
            .env_allowlist(["SIMPLICIO_TEST"])
            .idempotency_key("one")
            .build()
            .expect("valid spec");
        let dict = spec.to_dict();
        assert_eq!(dict["shell"], false);
        assert_eq!(dict["schema"], PROCESS_SPEC_SCHEMA);
        assert!(dict["spec_hash"].as_str().unwrap().len() == 64);
    }

    #[test]
    fn spec_hash_is_deterministic_and_order_independent() {
        let a = ProcessSpec::builder(["echo", "hi"])
            .env("B", "2")
            .env("A", "1")
            .env_allowlist(["B", "A"])
            .build()
            .unwrap();
        let b = ProcessSpec::builder(["echo", "hi"])
            .env("A", "1")
            .env("B", "2")
            .env_allowlist(["A", "B"])
            .build()
            .unwrap();
        assert_eq!(a.spec_hash(), b.spec_hash());
    }

    #[test]
    fn spec_hash_changes_with_argv() {
        let a = ProcessSpec::builder(["echo", "hi"]).build().unwrap();
        let b = ProcessSpec::builder(["echo", "bye"]).build().unwrap();
        assert_ne!(a.spec_hash(), b.spec_hash());
    }
}
