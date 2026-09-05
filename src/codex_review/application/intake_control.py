"""Bounded, owner-scoped pauses for restart without discarding accepted reviews."""

import asyncio


class IntakeControl:
    def __init__(self) -> None:
        self.operation_id: str | None = None
        self.active_jobs = 0
        self._expiry: asyncio.Task[None] | None = None

    def pause(self, operation_id: str, lease_seconds: float) -> bool:
        if self.operation_id is not None:
            return False
        self.operation_id = operation_id
        self._expiry = asyncio.create_task(self._expire(operation_id, lease_seconds))
        return True

    def resume(self, operation_id: str) -> bool:
        if self.operation_id != operation_id:
            return False
        self.operation_id = None
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        return True

    async def _expire(self, operation_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.operation_id == operation_id:
            self.operation_id = None
            self._expiry = None

    async def close(self) -> None:
        task = self._expiry
        if self.operation_id is not None:
            self.resume(self.operation_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
