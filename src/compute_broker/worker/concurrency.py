"""Small explicit owner registry; bounded tasks, with one operation per owner."""

from __future__ import annotations

import asyncio
from uuid import UUID


class WorkerCapacityError(RuntimeError):
    pass


class PrincipalScheduler:
    def __init__(self, *, max_concurrency=4, max_pending=16, max_principals=64):
        for value in (max_concurrency, max_pending, max_principals):
            if type(value) is not int or not 1 <= value <= 256:
                raise ValueError("worker concurrency bounds must be integers from one through 256")
        if max_pending < max_concurrency:
            raise ValueError("pending bound must cover active operations")
        self._capacity = asyncio.Semaphore(max_concurrency)
        self._owners = {}
        self._max_principals, self._max_pending, self._pending = max_principals, max_pending, 0

    def register(self, owner_id):
        owner_id = str(UUID(str(owner_id)))
        if owner_id not in self._owners:
            if len(self._owners) >= self._max_principals:
                raise WorkerCapacityError("Owner registry is full")
            self._owners[owner_id] = asyncio.Lock()
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
            # All operations serialize, which is stronger than mutation-only.
            async with self._owners[owner_id]:
                async with self._capacity:
                    return await operation()
        finally:
            self._pending -= 1
