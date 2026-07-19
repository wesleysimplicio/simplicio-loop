"""Event-driven bounded asynchronous queue with explicit backpressure.

No worker task, polling loop, or background thread is created by this module.
Producers wait on an asyncio condition and consumers are notified on state
transitions, so idle CPU is bounded by the event loop itself.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Tuple


QUEUE_SCHEMA = "simplicio.async-bounded-queue/v1"


class QueueClosed(RuntimeError):
    """The queue has been closed and cannot accept new items."""


class BackpressureError(RuntimeError):
    """A bounded queue rejected or timed out a producer."""

    def __init__(self, receipt: Dict[str, Any]) -> None:
        self.receipt = dict(receipt)
        super().__init__(str(self.receipt.get("reason", "backpressure")))


@dataclass(frozen=True)
class QueueItem:
    value: Any
    size: int
    key: Optional[str] = None
    enqueued_at: float = 0.0


class AsyncBoundedQueue:
    """A bounded queue using conditions/events rather than polling."""

    def __init__(
        self,
        max_items: int,
        *,
        max_bytes: int = 0,
        overload: str = "wait",
        coalesce: bool = False,
    ) -> None:
        if max_items < 1 or max_bytes < 0:
            raise ValueError("queue limits must be positive/non-negative")
        if overload not in {"wait", "reject"}:
            raise ValueError("overload must be wait or reject")
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.overload = overload
        self.coalesce = coalesce
        self._items: Deque[QueueItem] = deque()
        self._bytes = 0
        self._unfinished = 0
        self._closed = False
        self._condition = asyncio.Condition()
        self._accepted = 0
        self._coalesced = 0
        self._rejected = 0
        self._wait_count = 0

    def _validate_size(self, size: int) -> None:
        if size < 0 or (self.max_bytes and size > self.max_bytes):
            raise ValueError("item size exceeds queue contract")

    def _full(self, size: int) -> bool:
        return (
            len(self._items) >= self.max_items
            or bool(self.max_bytes and self._bytes + size > self.max_bytes)
        )

    def _receipt(self, reason: str, size: int, wait_ms: int = 0) -> Dict[str, Any]:
        return {
            "schema": "simplicio.async-backpressure/v1",
            "reason": reason,
            "requested_size": size,
            "queue": QUEUE_SCHEMA,
            "queued_items": len(self._items),
            "queued_bytes": self._bytes,
            "wait_ms": wait_ms,
        }

    def _find_key(self, key: str) -> Optional[int]:
        for index, item in enumerate(self._items):
            if item.key == key:
                return index
        return None

    async def put(
        self,
        value: Any,
        *,
        size: int = 1,
        key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._validate_size(size)
        started = time.monotonic()
        async with self._condition:
            if self._closed:
                raise QueueClosed("queue is closed")
            if self.coalesce and key is not None:
                index = self._find_key(key)
                if index is not None:
                    prior = self._items[index]
                    self._items[index] = QueueItem(value, size, key, prior.enqueued_at)
                    self._bytes += size - prior.size
                    self._coalesced += 1
                    self._condition.notify_all()
                    return {"accepted": True, "coalesced": True, "wait_ms": 0}
            while self._full(size):
                if self._closed:
                    raise QueueClosed("queue is closed")
                if self.overload == "reject":
                    self._rejected += 1
                    raise BackpressureError(self._receipt("full", size))
                self._wait_count += 1
                try:
                    if timeout is None:
                        await self._condition.wait()
                    else:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    self._rejected += 1
                    wait_ms = int((time.monotonic() - started) * 1000)
                    raise BackpressureError(self._receipt("timeout", size, wait_ms)) from exc
            item = QueueItem(value, size, key, time.monotonic())
            self._items.append(item)
            self._bytes += size
            self._unfinished += 1
            self._accepted += 1
            self._condition.notify_all()
            return {
                "accepted": True,
                "coalesced": False,
                "wait_ms": int((time.monotonic() - started) * 1000),
            }

    async def get(self) -> Tuple[Any, int, Optional[str]]:
        async with self._condition:
            while not self._items:
                if self._closed:
                    raise QueueClosed("queue is closed and empty")
                await self._condition.wait()
            item = self._items.popleft()
            self._bytes -= item.size
            self._condition.notify_all()
            return item.value, item.size, item.key

    def task_done(self) -> None:
        if self._unfinished < 1:
            raise ValueError("task_done called too many times")
        self._unfinished -= 1
        if self._unfinished == 0:
            self._wake_async()

    def _wake_async(self) -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self._notify)

    def _notify(self) -> None:
        async def notify() -> None:
            async with self._condition:
                self._condition.notify_all()
        asyncio.create_task(notify())

    async def join(self) -> None:
        async with self._condition:
            while self._unfinished:
                await self._condition.wait()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def reopen(self) -> None:
        async with self._condition:
            if self._items or self._unfinished:
                raise RuntimeError("cannot reopen while work is pending")
            self._closed = False
            self._condition.notify_all()

    def status(self) -> Dict[str, Any]:
        return {
            "schema": QUEUE_SCHEMA,
            "closed": self._closed,
            "items": len(self._items),
            "bytes": self._bytes,
            "unfinished": self._unfinished,
            "accepted": self._accepted,
            "coalesced": self._coalesced,
            "rejected": self._rejected,
            "wait_count": self._wait_count,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "overload": self.overload,
        }
