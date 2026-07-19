use std::collections::BTreeMap;
use std::path::Path;
use std::process::Stdio;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::time::timeout;

pub const PROCESS_SPEC_SCHEMA: &str = "simplicio.process-spec/v1";
pub const PROCESS_RESULT_SCHEMA: &str = "simplicio.process-result/v1";

#[derive(Debug, Deserialize)]
pub struct ProcessSpec {
    #[serde(default)]
    pub schema: Option<String>,
    pub argv: Vec<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub env_allowlist: Vec<String>,
    #[serde(default, alias = "timeout_seconds")]
    pub deadline_seconds: Option<f64>,
    #[serde(default = "default_max_output_bytes")]
    pub max_output_bytes: usize,
    #[serde(default)]
    pub allowed_cwd_root: Option<String>,
    #[serde(default)]
    pub stdio: Option<String>,
}

fn default_max_output_bytes() -> usize {
    65536
}

#[derive(Debug, Serialize)]
pub struct ProcessResult {
    pub schema: &'static str,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub duration_ms: u128,
    pub truncated: bool,
    pub error: Option<String>,
}

#[derive(Debug)]
pub enum SpecError {
    EmptyArgv,
    EnvOutsideAllowlist(String),
    CwdOutsideAllowedRoot,
    CwdNotAbsolute,
}

impl std::fmt::Display for SpecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SpecError::EmptyArgv => write!(f, "argv must contain a non-empty executable"),
            SpecError::EnvOutsideAllowlist(key) => {
                write!(f, "env key '{key}' is outside env_allowlist")
            }
            SpecError::CwdOutsideAllowedRoot => write!(f, "cwd is outside allowed_cwd_root"),
            SpecError::CwdNotAbsolute => write!(f, "cwd must be absolute"),
        }
    }
}

pub fn validate(spec: &ProcessSpec) -> Result<(), SpecError> {
    if spec.argv.is_empty() || spec.argv.iter().any(|value| value.is_empty()) {
        return Err(SpecError::EmptyArgv);
    }
    for key in spec.env.keys() {
        if !spec.env_allowlist.iter().any(|allowed| allowed == key) {
            return Err(SpecError::EnvOutsideAllowlist(key.clone()));
        }
    }
    if let Some(cwd) = &spec.cwd {
        if !Path::new(cwd).is_absolute() {
            return Err(SpecError::CwdNotAbsolute);
        }
        if let Some(root) = &spec.allowed_cwd_root {
            let root_path = Path::new(root);
            if !Path::new(cwd).starts_with(root_path) {
                return Err(SpecError::CwdOutsideAllowedRoot);
            }
        }
    }
    Ok(())
}

fn filtered_env(spec: &ProcessSpec) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for key in &spec.env_allowlist {
        if let Ok(value) = std::env::var(key) {
            out.push((key.clone(), value));
        }
    }
    for (key, value) in &spec.env {
        out.retain(|(existing, _)| existing != key);
        out.push((key.clone(), value.clone()));
    }
    out
}

fn bounded(raw: &[u8], limit: usize) -> (String, bool) {
    let truncated = raw.len() > limit;
    let slice = &raw[..raw.len().min(limit)];
    (String::from_utf8_lossy(slice).into_owned(), truncated)
}

/// Deserializes a `ProcessSpec` from a raw JSON string. Extracted from `main.rs` so the
/// stdin-parsing logic runs (and is measured) inside the crate's own test harness instead of
/// only through an external-process integration test that coverage tooling can't credit.
pub fn parse_spec(raw: &str) -> Result<ProcessSpec, serde_json::Error> {
    serde_json::from_str(raw)
}

/// Serializes a `ProcessResult` back to a JSON line. Mirrors `parse_spec`'s rationale.
pub fn serialize_result(result: &ProcessResult) -> String {
    serde_json::to_string(result).expect("ProcessResult always serializes")
}

pub async fn run(spec: &ProcessSpec) -> ProcessResult {
    let started = Instant::now();
    if let Err(err) = validate(spec) {
        return ProcessResult {
            schema: PROCESS_RESULT_SCHEMA,
            exit_code: None,
            stdout: String::new(),
            stderr: String::new(),
            timed_out: false,
            duration_ms: started.elapsed().as_millis(),
            truncated: false,
            error: Some(err.to_string()),
        };
    }

    let mut command = Command::new(&spec.argv[0]);
    command.args(&spec.argv[1..]);
    command.env_clear();
    for (key, value) in filtered_env(spec) {
        command.env(key, value);
    }
    if let Some(cwd) = &spec.cwd {
        command.current_dir(cwd);
    }
    command.stdin(Stdio::null());
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());

    #[cfg(unix)]
    {
        #[allow(unused_imports)]
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(|| {
                libc_setsid();
                Ok(())
            });
        }
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            return ProcessResult {
                schema: PROCESS_RESULT_SCHEMA,
                exit_code: None,
                stdout: String::new(),
                stderr: String::new(),
                timed_out: false,
                duration_ms: started.elapsed().as_millis(),
                truncated: false,
                error: Some(format!("spawn_error: {err}")),
            };
        }
    };

    let mut stdout_pipe = child.stdout.take();
    let mut stderr_pipe = child.stderr.take();
    let max_bytes = spec.max_output_bytes;

    let read_output = async {
        let mut out_buf = Vec::new();
        let mut err_buf = Vec::new();
        if let Some(pipe) = stdout_pipe.as_mut() {
            let _ = pipe
                .take(max_bytes as u64 + 1)
                .read_to_end(&mut out_buf)
                .await;
        }
        if let Some(pipe) = stderr_pipe.as_mut() {
            let _ = pipe
                .take(max_bytes as u64 + 1)
                .read_to_end(&mut err_buf)
                .await;
        }
        let status = child.wait().await;
        (status, out_buf, err_buf)
    };

    let deadline = spec.deadline_seconds.map(Duration::from_secs_f64);

    let outcome = match deadline {
        None => Some(read_output.await),
        Some(duration) => timeout(duration, read_output).await.ok(),
    };

    match outcome {
        Some((status, out_buf, err_buf)) => {
            let (stdout, out_truncated) = bounded(&out_buf, max_bytes);
            let (stderr, err_truncated) = bounded(&err_buf, max_bytes);
            ProcessResult {
                schema: PROCESS_RESULT_SCHEMA,
                exit_code: status.ok().and_then(|s| s.code()),
                stdout,
                stderr,
                timed_out: false,
                duration_ms: started.elapsed().as_millis(),
                truncated: out_truncated || err_truncated,
                error: None,
            }
        }
        None => {
            kill_tree(&mut child);
            let _ = child.wait().await;
            ProcessResult {
                schema: PROCESS_RESULT_SCHEMA,
                exit_code: None,
                stdout: String::new(),
                stderr: String::new(),
                timed_out: true,
                duration_ms: started.elapsed().as_millis(),
                truncated: false,
                error: Some("deadline_exceeded".to_string()),
            }
        }
    }
}

#[cfg(unix)]
fn libc_setsid() {
    unsafe {
        libc::setsid();
    }
}

/// On deadline expiry the child was started with `setsid()`, so its pid is also its own
/// process-group id: `killpg` tears down the whole tree (a shell that forked descendants), not
/// just the direct child, which is all `child.start_kill()` would reach. Non-Unix targets fall
/// back to killing just the top-level process.
#[cfg(unix)]
fn kill_tree(child: &mut tokio::process::Child) {
    if let Some(pid) = child.id() {
        unsafe {
            libc::killpg(pid as libc::pid_t, libc::SIGKILL);
        }
    } else {
        let _ = child.start_kill();
    }
}

#[cfg(not(unix))]
fn kill_tree(child: &mut tokio::process::Child) {
    let _ = child.start_kill();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(argv: Vec<&str>) -> ProcessSpec {
        ProcessSpec {
            schema: None,
            argv: argv.into_iter().map(String::from).collect(),
            cwd: None,
            env: BTreeMap::new(),
            env_allowlist: Vec::new(),
            deadline_seconds: None,
            max_output_bytes: 65536,
            allowed_cwd_root: None,
            stdio: None,
        }
    }

    #[tokio::test]
    async fn fast_command_completes_normally() {
        let result = run(&spec(fast_command())).await;
        assert_eq!(result.exit_code, Some(0));
        assert!(result.stdout.contains("hello"));
        assert!(!result.timed_out);
        assert!(result.error.is_none());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn overrunning_command_is_killed_at_deadline() {
        let mut s = spec(slow_command());
        s.deadline_seconds = Some(0.2);
        let started = Instant::now();
        let result = run(&s).await;
        assert!(result.timed_out);
        assert_eq!(result.error.as_deref(), Some("deadline_exceeded"));
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn deadline_kills_the_whole_process_tree_not_just_the_direct_child() {
        let marker = std::env::temp_dir().join(format!(
            "simplicio-supervisor-grandchild-{}-{}",
            std::process::id(),
            Instant::now().elapsed().as_nanos()
        ));
        let marker_str = marker.to_str().unwrap().to_string();
        let _ = std::fs::remove_file(&marker);

        let mut s = spec(vec![
            "sh",
            "-c",
            &format!(
                "(sleep 5; echo alive > {marker}) & echo $! > {marker}.pid; wait",
                marker = marker_str
            ),
        ]);
        s.deadline_seconds = Some(0.3);

        let result = run(&s).await;
        assert!(result.timed_out);

        let grandchild_pid: i32 = std::fs::read_to_string(format!("{marker_str}.pid"))
            .expect("shell wrote grandchild pid before deadline")
            .trim()
            .parse()
            .expect("pid file contains an integer");

        // Give the kernel time to finish reaping (a just-killed process is briefly a zombie, so
        // `kill(pid, 0)` can still report it as present for a short window even after `killpg`
        // succeeded). What actually proves the tree was torn down is that the grandchild never
        // reaches its 5s sleep-then-write — the marker file must not appear even well past that.
        tokio::time::sleep(Duration::from_millis(700)).await;

        let is_dead_or_zombie = std::fs::read_to_string(format!("/proc/{grandchild_pid}/stat"))
            .map(|stat| stat.contains(") Z "))
            .unwrap_or(true);
        assert!(
            is_dead_or_zombie,
            "grandchild pid {grandchild_pid} is still running 700ms after the deadline; process group was not fully torn down"
        );
        assert!(
            !marker.exists(),
            "grandchild ran to completion after the deadline instead of being killed"
        );

        let _ = std::fs::remove_file(&marker);
        let _ = std::fs::remove_file(format!("{marker_str}.pid"));
    }

    #[tokio::test]
    async fn env_allowlist_filters_ambient_and_explicit_vars() {
        std::env::set_var("SIMPLICIO_SUPERVISOR_TEST_AMBIENT", "should-not-appear");
        let mut s = spec(env_command());
        s.env.insert(
            "SIMPLICIO_SUPERVISOR_TEST_ALLOWED".to_string(),
            "yes".to_string(),
        );
        s.env_allowlist = vec!["SIMPLICIO_SUPERVISOR_TEST_ALLOWED".to_string()];
        let result = run(&s).await;
        assert!(result
            .stdout
            .contains("SIMPLICIO_SUPERVISOR_TEST_ALLOWED=yes"));
        assert!(!result.stdout.contains("SIMPLICIO_SUPERVISOR_TEST_AMBIENT"));
        std::env::remove_var("SIMPLICIO_SUPERVISOR_TEST_AMBIENT");
    }

    #[test]
    fn env_key_outside_allowlist_is_rejected() {
        let mut s = spec(vec!["echo", "x"]);
        s.env.insert("NOT_ALLOWED".to_string(), "1".to_string());
        assert!(matches!(
            validate(&s),
            Err(SpecError::EnvOutsideAllowlist(_))
        ));
    }

    #[test]
    fn cwd_outside_allowed_root_is_rejected() {
        let mut s = spec(vec!["echo", "x"]);
        s.cwd = Some(if cfg!(windows) { "C:\\Windows" } else { "/etc" }.to_string());
        s.allowed_cwd_root = Some(
            if cfg!(windows) {
                "C:\\Users"
            } else {
                "/home/user"
            }
            .to_string(),
        );
        assert!(matches!(
            validate(&s),
            Err(SpecError::CwdOutsideAllowedRoot)
        ));
    }

    #[test]
    fn cwd_inside_allowed_root_is_accepted() {
        let mut s = spec(vec!["echo", "x"]);
        s.cwd = Some(
            if cfg!(windows) {
                "C:\\Users\\user\\project"
            } else {
                "/home/user/project"
            }
            .to_string(),
        );
        s.allowed_cwd_root = Some(
            if cfg!(windows) {
                "C:\\Users"
            } else {
                "/home/user"
            }
            .to_string(),
        );
        assert!(validate(&s).is_ok());
    }

    #[tokio::test]
    async fn allowlisted_ambient_var_passes_through_without_being_in_spec_env() {
        std::env::set_var("SIMPLICIO_SUPERVISOR_TEST_AMBIENT_ALLOWED", "ambient-value");
        let mut s = spec(env_command());
        s.env_allowlist = vec!["SIMPLICIO_SUPERVISOR_TEST_AMBIENT_ALLOWED".to_string()];
        let result = run(&s).await;
        assert!(result
            .stdout
            .contains("SIMPLICIO_SUPERVISOR_TEST_AMBIENT_ALLOWED=ambient-value"));
        std::env::remove_var("SIMPLICIO_SUPERVISOR_TEST_AMBIENT_ALLOWED");
    }

    #[tokio::test]
    async fn run_honors_an_explicit_cwd() {
        let dir = std::env::temp_dir();
        let mut s = spec(if cfg!(windows) {
            vec!["C:\\Windows\\System32\\cmd.exe", "/C", "cd"]
        } else {
            vec!["pwd"]
        });
        s.cwd = Some(dir.to_str().unwrap().to_string());
        let result = run(&s).await;
        assert_eq!(result.exit_code, Some(0));
        let canonical_dir = std::fs::canonicalize(&dir).unwrap();
        let printed = std::path::Path::new(result.stdout.trim());
        assert_eq!(std::fs::canonicalize(printed).unwrap(), canonical_dir);
    }

    #[test]
    fn empty_argv_is_rejected() {
        let s = spec(vec![]);
        assert!(matches!(validate(&s), Err(SpecError::EmptyArgv)));
    }

    #[test]
    fn argv_with_blank_executable_is_rejected() {
        let s = spec(vec![""]);
        assert!(matches!(validate(&s), Err(SpecError::EmptyArgv)));
    }

    #[test]
    fn cwd_not_absolute_is_rejected() {
        let mut s = spec(vec!["echo", "x"]);
        s.cwd = Some("relative/path".to_string());
        assert!(matches!(validate(&s), Err(SpecError::CwdNotAbsolute)));
    }

    #[test]
    fn spec_error_display_messages_are_human_readable() {
        assert_eq!(
            SpecError::EmptyArgv.to_string(),
            "argv must contain a non-empty executable"
        );
        assert_eq!(
            SpecError::EnvOutsideAllowlist("FOO".to_string()).to_string(),
            "env key 'FOO' is outside env_allowlist"
        );
        assert_eq!(
            SpecError::CwdOutsideAllowedRoot.to_string(),
            "cwd is outside allowed_cwd_root"
        );
        assert_eq!(
            SpecError::CwdNotAbsolute.to_string(),
            "cwd must be absolute"
        );
    }

    #[tokio::test]
    async fn run_reports_a_validation_error_without_spawning() {
        let result = run(&spec(vec![])).await;
        assert!(result.exit_code.is_none());
        assert!(!result.timed_out);
        assert_eq!(
            result.error.as_deref(),
            Some("argv must contain a non-empty executable")
        );
    }

    #[tokio::test]
    async fn nonexistent_executable_reports_a_spawn_error() {
        let result = run(&spec(vec!["simplicio-supervisor-definitely-not-a-real-binary"])).await;
        assert!(result.exit_code.is_none());
        assert!(!result.timed_out);
        let err = result.error.expect("spawn failure surfaces as an error");
        assert!(err.starts_with("spawn_error:"), "unexpected error: {err}");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn output_larger_than_the_limit_is_truncated() {
        let mut s = spec(vec!["head", "-c", "4096", "/dev/zero"]);
        s.max_output_bytes = 16;
        let result = run(&s).await;
        assert_eq!(result.exit_code, Some(0));
        assert!(result.truncated);
        assert!(result.stdout.len() <= s.max_output_bytes);
    }

    #[test]
    fn parse_spec_round_trips_a_valid_payload() {
        let raw = r#"{"argv": ["echo", "hi"], "env_allowlist": ["PATH"]}"#;
        let parsed = parse_spec(raw).expect("valid JSON parses");
        assert_eq!(parsed.argv, vec!["echo".to_string(), "hi".to_string()]);
        assert_eq!(parsed.env_allowlist, vec!["PATH".to_string()]);
        assert_eq!(parsed.max_output_bytes, default_max_output_bytes());
    }

    #[test]
    fn parse_spec_rejects_invalid_json() {
        assert!(parse_spec("not json").is_err());
        assert!(parse_spec(r#"{"argv": "not-an-array"}"#).is_err());
    }

    #[tokio::test]
    async fn serialize_result_produces_valid_json_for_run_output() {
        let result = run(&spec(fast_command())).await;
        let body = serialize_result(&result);
        let round_tripped: serde_json::Value =
            serde_json::from_str(&body).expect("serialize_result output is valid JSON");
        assert_eq!(round_tripped["schema"], PROCESS_RESULT_SCHEMA);
    }

    fn fast_command() -> Vec<&'static str> {
        if cfg!(windows) {
            vec!["C:\\Windows\\System32\\cmd.exe", "/C", "echo", "hello"]
        } else {
            vec!["echo", "hello"]
        }
    }

    #[cfg(unix)]
    fn slow_command() -> Vec<&'static str> {
        vec!["sleep", "5"]
    }

    fn env_command() -> Vec<&'static str> {
        if cfg!(windows) {
            vec!["C:\\Windows\\System32\\cmd.exe", "/C", "set"]
        } else {
            vec!["env"]
        }
    }
}
