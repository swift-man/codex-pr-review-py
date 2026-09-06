import asyncio
from unittest.mock import AsyncMock

import pytest

from codex_review.application.webhook_handler import WebhookHandler

OPERATION = "a" * 64


def payload(number: int = 1) -> dict:
    return {
        "action": "opened", "pull_request": {"number": number},
        "repository": {"full_name": "owner/repo"}, "installation": {"id": 1},
    }


def build_handler() -> WebhookHandler:
    return WebhookHandler(secret="webhook", github=AsyncMock(), use_case=AsyncMock())


async def test_drain_finishes_all_accepted_reviews_without_dropping_queue() -> None:
    handler = build_handler()
    started = asyncio.Event()
    finish = asyncio.Event()
    completed: list[int] = []

    async def process(job) -> None:
        started.set()
        await finish.wait()
        completed.append(job.number)

    handler._process = process
    for number in (1, 2, 3):
        assert await handler.accept("pull_request", str(number), payload(number)) == (202, "queued")
    await handler.start()
    await asyncio.wait_for(started.wait(), 1)
    draining = asyncio.create_task(handler.drain(OPERATION, timeout=1))
    try:
        await asyncio.sleep(0)
        assert handler.intake.active_jobs == 1
        assert handler.queue_depth == 2
        assert not draining.done()
        assert await handler.accept("pull_request", "new", payload(4)) == (503, "draining")
        finish.set()
        assert await draining
        assert completed == [1, 2, 3]
        assert handler.queue_depth == handler.intake.active_jobs == 0
    finally:
        finish.set()
        await handler.stop()


async def test_drain_timeout_resumes_intake_and_preserves_accepted_job() -> None:
    handler = build_handler()
    await handler.accept("pull_request", "before", payload())
    with pytest.raises(TimeoutError):
        await handler.drain(OPERATION, timeout=0)
    assert handler.intake.operation_id is None
    assert handler.queue_depth == 1
    assert await handler.accept("pull_request", "after", payload(2)) == (202, "queued")
    await handler.stop()


async def test_accept_waiting_for_publisher_identity_cannot_enqueue_after_drain() -> None:
    handler = build_handler()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def discover() -> None:
        started.set()
        await finish.wait()

    handler._github.ensure_bot_login = discover
    accepting = asyncio.create_task(handler.accept("pull_request", "race", payload()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await handler.drain(OPERATION)
        finish.set()
        assert await accepting == (503, "draining")
        assert handler.queue_depth == 0
    finally:
        finish.set()
        await handler.stop()


async def test_cancelled_drain_resumes_intake_without_discarding_work() -> None:
    handler = build_handler()
    await handler.accept("pull_request", "queued", payload())
    draining = asyncio.create_task(handler.drain(OPERATION))
    await asyncio.sleep(0)
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert handler.intake.operation_id is None
    assert handler.queue_depth == 1
    await handler.stop()
