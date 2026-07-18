//! Renewable, cancellable process lease.
//!
//! Mirrors `simplicio_loop/process_supervisor.py::ProcessLease`: a lease starts `Active`,
//! `heartbeat()` pushes its expiry forward, `expired()` transitions it to `Expired` once its TTL
//! elapses, and `cancel()` moves it to a terminal `Cancelled` state. Time is injected (`Instant`)
//! rather than read from the clock internally so tests are deterministic, the same role played by
//! the optional `now=` keyword on the Python side.

use std::time::{Duration, Instant};

use crate::spec::ProcessSpecError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseState {
    Active,
    Expired,
    Cancelled,
}

#[derive(Debug, Clone)]
pub struct ProcessLease {
    pub lease_id: String,
    pub spec_hash: String,
    pub ttl_seconds: f64,
    pub expires_at: Instant,
    pub state: LeaseState,
}

impl ProcessLease {
    /// Create an active lease expiring `ttl_seconds` after `now`.
    pub fn new(
        lease_id: impl Into<String>,
        spec_hash: impl Into<String>,
        ttl_seconds: f64,
        now: Instant,
    ) -> Result<Self, ProcessSpecError> {
        if ttl_seconds <= 0.0 {
            return Err(ProcessSpecError::new("lease ttl must be positive"));
        }
        Ok(Self {
            lease_id: lease_id.into(),
            spec_hash: spec_hash.into(),
            ttl_seconds,
            expires_at: now + Duration::from_secs_f64(ttl_seconds),
            state: LeaseState::Active,
        })
    }

    /// Push expiry forward by `ttl_seconds` from `now`; a no-op once the lease left `Active`.
    pub fn heartbeat(&mut self, now: Instant) -> Instant {
        if self.state != LeaseState::Active {
            return self.expires_at;
        }
        self.expires_at = now + Duration::from_secs_f64(self.ttl_seconds);
        self.expires_at
    }

    /// Transition to `Expired` (sticky) if `now` has passed `expires_at` while still `Active`.
    pub fn expired(&mut self, now: Instant) -> bool {
        if self.state == LeaseState::Active && now >= self.expires_at {
            self.state = LeaseState::Expired;
        }
        self.state == LeaseState::Expired
    }

    pub fn cancel(&mut self) {
        self.state = LeaseState::Cancelled;
    }

    pub fn is_cancelled(&self) -> bool {
        self.state == LeaseState::Cancelled
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_expiry_and_cancel_transitions() {
        let t0 = Instant::now();
        let mut lease = ProcessLease::new("lease-1", "spec-1", 5.0, t0).unwrap();

        assert!(!lease.expired(t0 + Duration::from_secs(4)));

        let renewed = lease.heartbeat(t0 + Duration::from_secs(20));
        assert_eq!(renewed, t0 + Duration::from_secs(25));

        assert!(!lease.expired(t0 + Duration::from_secs(24)));
        assert!(lease.expired(t0 + Duration::from_secs(25)));
        assert_eq!(lease.state, LeaseState::Expired);

        lease.cancel();
        assert_eq!(lease.state, LeaseState::Cancelled);
        assert!(lease.is_cancelled());
    }

    #[test]
    fn rejects_non_positive_ttl() {
        let err = ProcessLease::new("lease-2", "spec-1", 0.0, Instant::now()).unwrap_err();
        assert_eq!(err.0, "lease ttl must be positive");
    }

    #[test]
    fn heartbeat_after_cancel_is_a_no_op() {
        let t0 = Instant::now();
        let mut lease = ProcessLease::new("lease-3", "spec-1", 5.0, t0).unwrap();
        lease.cancel();
        let before = lease.expires_at;
        let after = lease.heartbeat(t0 + Duration::from_secs(100));
        assert_eq!(before, after);
    }
}
