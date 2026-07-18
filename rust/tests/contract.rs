//! Contract round-trip tests — `ProcessSpec` / `ProcessLease` / `ProcessResult` serialize to the
//! same shape as the Python contract from #514
//! (`simplicio_loop/process_supervisor.py`): same schema strings, same field names, `shell` is
//! always `false` on the wire.

use std::time::{Duration, Instant};

use simplicio_supervisor::{
    LeaseState, ProcessLease, ProcessResult, ProcessSpec, PROCESS_RESULT_SCHEMA,
    PROCESS_SPEC_SCHEMA,
};

#[test]
fn process_spec_round_trips_through_json_with_the_python_contract_shape() {
    let spec = ProcessSpec::builder(["/usr/bin/env", "true"])
        .cwd("/tmp")
        .env("SIMPLICIO_TEST", "1")
        .env_allowlist(["SIMPLICIO_TEST"])
        .timeout_seconds(Some(12.5))
        .max_output_bytes(4096)
        .priority(3)
        .idempotency_key("contract-roundtrip")
        .build()
        .expect("valid spec");

    let dict = spec.to_dict();
    assert_eq!(dict["schema"], PROCESS_SPEC_SCHEMA);
    assert_eq!(dict["argv"], serde_json::json!(["/usr/bin/env", "true"]));
    assert_eq!(dict["cwd"], "/tmp");
    assert_eq!(dict["env"]["SIMPLICIO_TEST"], "1");
    assert_eq!(dict["env_allowlist"], serde_json::json!(["SIMPLICIO_TEST"]));
    assert_eq!(dict["timeout_seconds"], 12.5);
    assert_eq!(dict["max_output_bytes"], 4096);
    assert_eq!(dict["priority"], 3);
    assert_eq!(dict["idempotency_key"], "contract-roundtrip");
    // Mirrors the Python contract's hard invariant: shell execution never reaches the wire.
    assert_eq!(dict["shell"], false);

    let round_tripped: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(&dict).unwrap()).unwrap();
    assert_eq!(round_tripped, dict);
}

#[test]
fn process_result_round_trips_and_carries_the_error_taxonomy() {
    let result = ProcessResult {
        returncode: Some(1),
        stdout: "partial".into(),
        stderr: String::new(),
        duration_seconds: 0.25,
        timed_out: true,
        cancelled: false,
        truncated: true,
        error_code: "deadline_exceeded".into(),
        lease_id: "lease-contract".into(),
    };
    let dict = result.to_dict();
    assert_eq!(dict["schema"], PROCESS_RESULT_SCHEMA);
    assert_eq!(dict["error_code"], "deadline_exceeded");
    assert_eq!(dict["truncated"], true);
    assert_eq!(dict["lease_id"], "lease-contract");

    let round_tripped: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(&dict).unwrap()).unwrap();
    assert_eq!(round_tripped, dict);
}

#[test]
fn lease_transitions_match_the_python_state_machine() {
    let t0 = Instant::now();
    let mut lease =
        ProcessLease::new("lease-contract", "spec-hash", 1.0, t0).expect("positive ttl accepted");
    assert!(matches!(lease.state, LeaseState::Active));

    // Active + not yet expired.
    assert!(!lease.expired(t0 + Duration::from_millis(500)));
    // Active -> Expired once past ttl.
    assert!(lease.expired(t0 + Duration::from_secs(2)));
    assert!(matches!(lease.state, LeaseState::Expired));

    // A fresh lease can instead be cancelled directly (Active -> Cancelled), a terminal state
    // heartbeat cannot revive.
    let mut cancellable = ProcessLease::new("lease-contract-2", "spec-hash", 5.0, t0).unwrap();
    cancellable.cancel();
    assert!(cancellable.is_cancelled());
    let stuck = cancellable.heartbeat(t0 + Duration::from_secs(100));
    assert_eq!(stuck, cancellable.expires_at);
}
