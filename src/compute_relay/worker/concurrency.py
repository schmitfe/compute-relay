"""Explicit owner registry with finite process and per-owner operation bounds."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID


class WorkerCapacityError(RuntimeError):
    pass


def remote_parallelism(value=None):
    if value is None:
        value = os.environ.get("COMPUTE_RELAY_MAX_REMOTE_OPERATIONS", "4")
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise ValueError("Remote operation concurrency must be an integer from 1 through 8") from None
    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError("Remote operation concurrency must be an integer from 1 through 8")
    return value


async def poll_parallel(poll, *, concurrency, once=False):
    """A fixed number of lanes; stop and drain every lane on error/shutdown."""
    concurrency = remote_parallelism(concurrency)
    async def lane():
        while True:
            worked = await poll()
            if once:
                return
            await asyncio.sleep(0 if worked else 2)
    tasks = [asyncio.create_task(lane()) for _ in range(1 if once else concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class PrincipalScheduler:
    def __init__(self, *, max_concurrency=4, max_pending=16, max_principals=64, max_per_owner=4):
        for value in (max_concurrency, max_pending, max_principals, max_per_owner):
            if type(value) is not int or not 1 <= value <= 256:
                raise ValueError("worker concurrency bounds must be integers from one through 256")
        if max_pending < max_concurrency:
            raise ValueError("pending bound must cover active operations")
        self._capacity = asyncio.Semaphore(max_concurrency)
        self._owners = {}
        self._max_per_owner = max_per_owner
        self._max_principals, self._max_pending, self._pending = max_principals, max_pending, 0

    def register(self, owner_id):
        owner_id = str(UUID(str(owner_id)))
        if owner_id not in self._owners:
            if len(self._owners) >= self._max_principals:
                raise WorkerCapacityError("Owner registry is full")
            self._owners[owner_id] = asyncio.Semaphore(self._max_per_owner)
        return owner_id

    async def run(self, owner_id, operation):
        owner_id = str(UUID(str(owner_id)))
        if owner_id not in self._owners:
            raise WorkerCapacityError("Owner is not registered by protected configuration")
        if self._pending >= self._max_pending:
            raise WorkerCapacityError("Worker operation capacity is full")
        self._pending += 1
        try:
            # Waiting operations from one owner cannot consume all global slots.
            async with self._owners[owner_id]:
                async with self._capacity:
                    return await operation()
        finally:
            self._pending -= 1
