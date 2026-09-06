"""Bounded, owner-scoped pauses for restart without discarding accepted reviews."""

import asyncio


class IntakeControl:
    def __init__(self) -> None:
        self.operation_id: str | None = None
        self.active_jobs = 0
        self.restart_committed = False
        self._expiry: asyncio.Task[None] | None = None

    def pause(self, operation_id: str, lease_seconds: float) -> bool:
        if self.operation_id is not None:
            return False
        self.operation_id = operation_id
        self._expiry = asyncio.create_task(self._expire(operation_id, lease_seconds))
        return True

    def resume(self, operation_id: str) -> bool:
        if self.operation_id != operation_id or self.restart_committed:
            return False
        self.operation_id = None
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        return True

    def commit_restart(self, operation_id: str) -> bool:
        if self.operation_id != operation_id or self.active_jobs:
            return False
        self.restart_committed = True
        if self._expiry is not None:
            self._expiry.cancel()
        return True

    async def _expire(self, operation_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.operation_id == operation_id and not self.restart_committed:
            self.operation_id = None
            self._expiry = None

    async def close(self) -> None:
        task = self._expiry
        if self.operation_id is not None:
            self.resume(self.operation_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
