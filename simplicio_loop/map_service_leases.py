"""Short-lived build-owner leases for Map Service single-flight work."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Dict, Optional


@dataclass(frozen=True)
class BuildLease:
    key: str
    token: str
    expires_at: float

    @property
    def ttl(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


class BuildLeaseTable:
    """Thread-safe lease election with deterministic stale-owner recovery."""

    def __init__(self) -> None:
        self._leases: Dict[str, BuildLease] = {}
        self._lock = RLock()

    def acquire(self, key: str, *, ttl: float, now: Optional[float] = None) -> Optional[BuildLease]:
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        current_time = time.monotonic() if now is None else float(now)
        key = str(key)
        with self._lock:
            current = self._leases.get(key)
            if current is not None and current.expires_at > current_time:
                return None
            lease = BuildLease(key, uuid.uuid4().hex, current_time + float(ttl))
            self._leases[key] = lease
            return lease

    def renew(self, lease: BuildLease, *, ttl: float, now: Optional[float] = None) -> BuildLease:
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        current_time = time.monotonic() if now is None else float(now)
        with self._lock:
            current = self._leases.get(lease.key)
            if current is None or current.token != lease.token or current.expires_at <= current_time:
                raise RuntimeError("lease is no longer owned")
            updated = BuildLease(lease.key, lease.token, current_time + float(ttl))
            self._leases[lease.key] = updated
            return updated

    def release(self, lease: BuildLease) -> bool:
        with self._lock:
            current = self._leases.get(lease.key)
            if current is None or current.token != lease.token:
                return False
            del self._leases[lease.key]
            return True

    def status(self, *, now: Optional[float] = None) -> Dict[str, int]:
        current_time = time.monotonic() if now is None else float(now)
        with self._lock:
            expired = sum(lease.expires_at <= current_time for lease in self._leases.values())
            return {"active": len(self._leases) - expired, "expired": expired}
