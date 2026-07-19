//! End-to-end tests that drive the compiled `simplicio-supervisor` binary the same way
//! `simplicio_loop/process_supervisor_rust.py` does: write a JSON `ProcessSpec` to stdin, read a
//! JSON `ProcessResult` from stdout. These exercise `main.rs`'s stdin/stdout wiring directly
//! (rather than only through an external Python integration test), so `main.rs` is measured by
//! the crate's own `cargo test`/`cargo tarpaulin` run.

use std::io::Write;
use std::process::{Command, Stdio};

fn binary_path() -> std::path::PathBuf {
    let mut path = std::env::current_exe().expect("current test exe path");
    path.pop(); // deps
    path.pop(); // profile dir
    let name = if cfg!(windows) {
        "simplicio-supervisor.exe"
    } else {
        "simplicio-supervisor"
    };
    path.join(name)
}

fn run_cli(stdin: &str) -> (std::process::ExitStatus, String, String) {
    let mut child = Command::new(binary_path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("simplicio-supervisor binary must be built (cargo test builds it first)");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    let output = child.wait_with_output().expect("child process runs");
    (
        output.status,
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn valid_spec_on_stdin_produces_a_process_result_on_stdout() {
    let (status, stdout, _stderr) = run_cli(r#"{"argv": ["echo", "hello-from-cli"]}"#);
    assert!(status.success());
    let value: serde_json::Value = serde_json::from_str(stdout.trim()).expect("valid JSON line");
    assert_eq!(value["schema"], "simplicio.process-result/v1");
    assert_eq!(value["exit_code"], 0);
    assert!(value["stdout"].as_str().unwrap().contains("hello-from-cli"));
}

#[test]
fn invalid_json_on_stdin_exits_nonzero_with_no_stdout() {
    let (status, stdout, stderr) = run_cli("not json");
    assert!(!status.success());
    assert_eq!(status.code(), Some(2));
    assert!(stdout.is_empty());
    assert!(stderr.contains("invalid ProcessSpec JSON"));
}
