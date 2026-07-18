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
            let _ = child.start_kill();
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
        extern "C" {
            fn setsid() -> i32;
        }
        setsid();
    }
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
