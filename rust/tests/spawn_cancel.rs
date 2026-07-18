//! Integration tests for the Tokio spawn/cancel adapter (Unix only).
//!
//! These exercise the real OS: real `fork`/`exec`, real signals, real files — no mocked process
//! layer, per the repo's "no mocking to hide a runtime/kernel failure" rule.

#![cfg(unix)]

use std::time::Duration;

use simplicio_supervisor::adapter::TokioProcessAdapter;
use simplicio_supervisor::spec::ProcessSpec;

fn is_alive(pid: i32) -> bool {
    // kill(pid, 0) sends no signal; it only checks whether the process still exists and is
    // reachable (ESRCH => gone).
    unsafe { libc::kill(pid, 0) == 0 }
}

#[tokio::test]
async fn spawn_runs_argv_and_captures_stdout() {
    let spec = ProcessSpec::builder(["/bin/sh", "-c", "echo hamt-ok"])
        .build()
        .expect("valid spec");
    let (_tx, rx) = tokio::sync::oneshot::channel();
    let result = TokioProcessAdapter::new()
        .run(&spec, "lease-echo", rx)
        .await;

    assert_eq!(result.returncode, Some(0));
    assert!(
        result.stdout.contains("hamt-ok"),
        "stdout was {:?}",
        result.stdout
    );
    assert!(!result.timed_out);
    assert!(!result.cancelled);
    assert_eq!(result.error_code, "");
}

#[tokio::test]
async fn cancel_kills_the_whole_process_tree_not_just_the_top_pid() {
    let dir = tempdir();
    let pid_file = dir.join("child.pid");

    // The TOP process (`sh`) forks a DESCENDANT (`sleep 30 &`) and writes its pid to a file,
    // then blocks in `wait`. Cancelling the lease must kill both — proving descendants are
    // reaped too, not just the direct child handle Rust holds.
    let script = format!(
        "sleep 30 & echo $! > {path}; wait",
        path = pid_file.display()
    );
    let spec = ProcessSpec::builder(vec!["/bin/sh".to_string(), "-c".to_string(), script])
        .timeout_seconds(None)
        .build()
        .expect("valid spec");

    let (tx, rx) = tokio::sync::oneshot::channel();
    let run = tokio::spawn(async move {
        TokioProcessAdapter::new()
            .run(&spec, "lease-tree", rx)
            .await
    });

    // Wait for the descendant to actually start and record its pid.
    let descendant_pid = wait_for_pid_file(&pid_file, Duration::from_secs(5)).await;
    assert!(
        is_alive(descendant_pid),
        "descendant should be running before cancel"
    );

    tx.send(()).expect("adapter still awaiting cancel");
    let result = tokio::time::timeout(Duration::from_secs(5), run)
        .await
        .expect("run should finish promptly after cancel")
        .expect("task should not panic");

    assert!(
        result.cancelled,
        "result should be classified as cancelled: {result:?}"
    );
    assert_eq!(result.error_code, "cancelled");

    // The core AC: the descendant (not the process Rust directly spawned) must be gone too,
    // and detected as gone, not left as an orphan.
    let descendant_gone =
        wait_for_condition(Duration::from_secs(5), || !is_alive(descendant_pid)).await;
    assert!(
        descendant_gone,
        "descendant pid {descendant_pid} should be dead after cancel"
    );
}

#[tokio::test]
async fn timeout_is_classified_as_deadline_exceeded_and_kills_the_process() {
    let spec = ProcessSpec::builder(["/bin/sh", "-c", "sleep 5"])
        .timeout_seconds(Some(0.05))
        .build()
        .expect("valid spec");
    let (_tx, rx) = tokio::sync::oneshot::channel();
    let result = tokio::time::timeout(
        Duration::from_secs(5),
        TokioProcessAdapter::new().run(&spec, "lease-timeout", rx),
    )
    .await
    .expect("adapter must not hang past its own deadline");

    assert!(result.timed_out);
    assert_eq!(result.error_code, "deadline_exceeded");
    assert!(!result.cancelled);
}

#[tokio::test]
async fn missing_executable_is_classified_without_spawning() {
    let spec = ProcessSpec::builder(["simplicio-no-such-executable-515"])
        .build()
        .expect("valid spec");
    let (_tx, rx) = tokio::sync::oneshot::channel();
    let result = TokioProcessAdapter::new()
        .run(&spec, "lease-missing", rx)
        .await;

    assert_eq!(result.returncode, None);
    assert_eq!(result.error_code, "executable_not_found");
}

#[tokio::test]
async fn env_allowlist_is_enforced_end_to_end() {
    // SIMPLICIO_TEST_SECRET is NOT in the allowlist and must not reach the child even though the
    // adapter's own process could see it via std::env if it were inherited wholesale.
    std::env::set_var("SIMPLICIO_TEST_SECRET", "should-not-leak");
    std::env::set_var("SIMPLICIO_TEST_ALLOWED", "should-pass-through");

    let spec = ProcessSpec::builder([
        "/bin/sh",
        "-c",
        "echo \"A=$SIMPLICIO_TEST_ALLOWED,S=$SIMPLICIO_TEST_SECRET\"",
    ])
    .env_allowlist(["SIMPLICIO_TEST_ALLOWED"])
    .build()
    .expect("valid spec");
    let (_tx, rx) = tokio::sync::oneshot::channel();
    let result = TokioProcessAdapter::new().run(&spec, "lease-env", rx).await;

    std::env::remove_var("SIMPLICIO_TEST_SECRET");
    std::env::remove_var("SIMPLICIO_TEST_ALLOWED");

    assert_eq!(result.returncode, Some(0));
    assert!(
        result.stdout.contains("A=should-pass-through"),
        "stdout was {:?}",
        result.stdout
    );
    assert!(
        !result.stdout.contains("should-not-leak"),
        "stdout leaked a non-allowlisted var: {:?}",
        result.stdout
    );
}

fn tempdir() -> std::path::PathBuf {
    let mut dir = std::env::temp_dir();
    let unique = format!(
        "simplicio-supervisor-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    dir.push(unique);
    std::fs::create_dir_all(&dir).expect("create tempdir");
    dir
}

async fn wait_for_pid_file(path: &std::path::Path, timeout: Duration) -> i32 {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if let Ok(contents) = std::fs::read_to_string(path) {
            if let Ok(pid) = contents.trim().parse::<i32>() {
                return pid;
            }
        }
        if tokio::time::Instant::now() >= deadline {
            panic!("pid file {} was not written in time", path.display());
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

async fn wait_for_condition<F: FnMut() -> bool>(timeout: Duration, mut condition: F) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if condition() {
            return true;
        }
        if tokio::time::Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}
