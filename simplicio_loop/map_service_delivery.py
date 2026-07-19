"""Portable snapshot delivery and pin-aware garbage collection."""

from __future__ import annotations

import hashlib
import mmap
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Dict, Iterator, List, Optional


class SnapshotLimitError(ValueError):
    """A snapshot exceeds the configured delivery limit."""


@dataclass
class _Snapshot:
    key: str
    path: Path
    size: int
    valid: bool = True
    pins: int = 0


class SnapshotHandle:
    """A pinned snapshot that can be read as mmap or bounded stream."""

    def __init__(self, store: "SnapshotDeliveryStore", snapshot: _Snapshot, mode: str) -> None:
        self._store, self._snapshot, self.mode = store, snapshot, mode
        self._closed = False
        self._mapping = None
        self._file = None
        if mode == "mmap":
            self._file = open(str(snapshot.path), "rb")
            self._mapping = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ) if snapshot.size else None

    @property
    def key(self) -> str:
        return self._snapshot.key

    @property
    def size(self) -> int:
        return self._snapshot.size

    def read(self) -> bytes:
        if self._closed:
            raise RuntimeError("snapshot handle is closed")
        if self._mapping is not None:
            return self._mapping[:]
        return self._snapshot.path.read_bytes()

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if self._closed:
            raise RuntimeError("snapshot handle is closed")
        with self._snapshot.path.open("rb") as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._mapping is not None:
            self._mapping.close()
        if self._file is not None:
            self._file.close()
        self._store.release(self._snapshot.key)

    def __enter__(self) -> "SnapshotHandle":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class SnapshotDeliveryStore:
    """Content-addressed files with explicit pins and safe GC."""

    def __init__(self, directory: Optional[str] = None, *, max_bytes: int = 128 * 1024 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.directory = Path(directory or tempfile.mkdtemp(prefix="simplicio-map-"))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self._snapshots: Dict[str, _Snapshot] = {}
        self._latest: Optional[str] = None
        self._lock = RLock()

    def publish(self, key: str, content: bytes) -> str:
        content = bytes(content)
        if len(content) > self.max_bytes:
            raise SnapshotLimitError("snapshot exceeds max_bytes")
        key = str(key)
        digest = hashlib.sha256(content).hexdigest()
        path = self.directory / (digest + ".snapshot")
        if not path.exists():
            fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=str(self.directory))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, str(path))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        with self._lock:
            self._snapshots[key] = _Snapshot(key, path, len(content))
            self._latest = key
        return key

    def acquire(self, key: str, *, prefer_mmap: bool = True) -> SnapshotHandle:
        with self._lock:
            snapshot = self._snapshots.get(str(key))
            if snapshot is None or not snapshot.valid:
                raise KeyError(key)
            snapshot.pins += 1
            mode = "mmap" if prefer_mmap and hasattr(mmap, "mmap") else "stream"
            return SnapshotHandle(self, snapshot, mode)

    def release(self, key: str) -> None:
        with self._lock:
            snapshot = self._snapshots.get(str(key))
            if snapshot is not None:
                snapshot.pins = max(0, snapshot.pins - 1)

    def invalidate(self, key: str) -> None:
        with self._lock:
            if str(key) in self._snapshots:
                self._snapshots[str(key)].valid = False

    def gc(self) -> List[str]:
        with self._lock:
            removed = []
            for key, snapshot in list(self._snapshots.items()):
                if snapshot.valid or snapshot.pins or key == self._latest:
                    continue
                try:
                    snapshot.path.unlink()
                except FileNotFoundError:
                    pass
                removed.append(key)
                del self._snapshots[key]
            return sorted(removed)

    def status(self) -> Dict[str, int]:
        with self._lock:
            return {
                "snapshots": len(self._snapshots),
                "pinned": sum(snapshot.pins for snapshot in self._snapshots.values()),
                "bytes": sum(snapshot.size for snapshot in self._snapshots.values()),
            }
