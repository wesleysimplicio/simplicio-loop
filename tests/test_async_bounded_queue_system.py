"""System-level lifecycle probe for the bounded queue command contract."""

import json
import subprocess
import sys


PROBE = r'''
import asyncio, json
from simplicio_loop.async_bounded_queue import AsyncBoundedQueue

async def main():
    queue = AsyncBoundedQueue(2, max_bytes=8)
    await queue.put("work", size=4)
    value, size, key = await queue.get()
    queue.task_done()
    await queue.join()
    await queue.close()
    print(json.dumps({"value": value, "size": size, "closed": queue.status()["closed"]}))

asyncio.run(main())
'''


def test_real_command_can_drain_close_and_restart_queue_process() -> None:
    results = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        results.append(json.loads(result.stdout))
    assert results == [
        {"value": "work", "size": 4, "closed": True},
        {"value": "work", "size": 4, "closed": True},
    ]
