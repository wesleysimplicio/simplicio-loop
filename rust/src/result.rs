//! Bounded, classified process outcome.
//!
//! Mirrors `simplicio_loop/process_supervisor.py::ProcessResult`: a fixed set of fields plus an
//! `error_code` taxonomy (`"deadline_exceeded"`, `"cancelled"`, `"executable_not_found"`,
//! `"spawn_error"`) so callers on either side of the Python/Rust boundary classify outcomes the
//! same way instead of parsing free-text errors.

use serde::Serialize;

pub const PROCESS_RESULT_SCHEMA: &str = "simplicio.process-result/v1";

#[derive(Debug, Clone, Serialize)]
pub struct ProcessResult {
    pub returncode: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub duration_seconds: f64,
    pub timed_out: bool,
    pub cancelled: bool,
    pub truncated: bool,
    pub error_code: String,
    pub lease_id: String,
}

impl Default for ProcessResult {
    fn default() -> Self {
        Self {
            returncode: None,
            stdout: String::new(),
            stderr: String::new(),
            duration_seconds: 0.0,
            timed_out: false,
            cancelled: false,
            truncated: false,
            error_code: String::new(),
            lease_id: String::new(),
        }
    }
}

impl ProcessResult {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "schema": PROCESS_RESULT_SCHEMA,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "error_code": self.error_code,
            "lease_id": self.lease_id,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_result_has_no_error_and_no_output() {
        let result = ProcessResult::default();
        assert_eq!(result.returncode, None);
        assert!(result.stdout.is_empty());
        assert!(!result.timed_out);
        assert!(!result.cancelled);
    }

    #[test]
    fn to_dict_carries_schema_and_fields() {
        let result = ProcessResult {
            returncode: Some(0),
            lease_id: "lease-9".into(),
            ..ProcessResult::default()
        };
        let dict = result.to_dict();
        assert_eq!(dict["schema"], PROCESS_RESULT_SCHEMA);
        assert_eq!(dict["returncode"], 0);
        assert_eq!(dict["lease_id"], "lease-9");
    }
}
