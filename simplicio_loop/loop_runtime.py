"""Small Python 3.8-compatible runtime for bounded structured work.

The runtime deliberately owns only task admission, cancellation, timeout, and
shutdown semantics. Queue persistence, leases, and remote IPC remain separate
operator concerns so this slice can be rolled out independently.
"""

import asyncio
from typing import Any, Awaitable, Callable, Optional, Set, TypeVar

T = TypeVar("T")
Operation = Callable[..., Awaitable[T]]


class LoopRuntime:
    """Bound concurrent async operations with explicit cancellation semantics."""

    def __init__(self, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._tasks: Set["asyncio.Task[Any]"] = set()
        self._shutdown_requested = False
        self._closed = False

    @property
    def active_tasks(self) -> int:
        """Return the number of admitted tasks still awaiting completion."""
        return len(self._tasks)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed or self._shutdown_requested:
            raise RuntimeError("LoopRuntime is shutting down")

    def _track(self, task: "asyncio.Task[Any]") -> "asyncio.Task[Any]":
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _execute(
        self, operation: Operation[T], *args: Any, **kwargs: Any
    ) -> T:
        self._ensure_open()
        async with self._semaphore:
            self._ensure_open()
            return await operation(*args, **kwargs)

    async def run(
        self,
        operation: Operation[T],
        *args: Any,
        timeout: Optional[float] = None,
        **kwargs: Any
    ) -> T:
        """Run one operation with bounded admission and optional timeout."""
        self._ensure_open()
        task = self._track(
            asyncio.create_task(self._execute(operation, *args, **kwargs))
        )
        if timeout is None:
            return await task
        return await asyncio.wait_for(task, timeout=timeout)

    def spawn(
        self, operation: Operation[T], *args: Any, **kwargs: Any
    ) -> "asyncio.Task[T]":
        """Schedule an operation and return its cancellable task handle."""
        self._ensure_open()
        return self._track(
            asyncio.create_task(self._execute(operation, *args, **kwargs))
        )

    def request_shutdown(self) -> None:
        """Request shutdown and cancel all currently admitted work."""
        if self._closed:
            return
        self._shutdown_requested = True
        for task in tuple(self._tasks):
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel outstanding work and wait until all tasks have settled."""
        if self._closed:
            return
        self.request_shutdown()
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._closed = True

    def run_sync(
        self,
        operation: Operation[T],
        *args: Any,
        timeout: Optional[float] = None,
        **kwargs: Any
    ) -> T:
        """Run one operation from synchronous code."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run(operation, *args, timeout=timeout, **kwargs)
            )
        raise RuntimeError("run_sync cannot be called from an active event loop")
