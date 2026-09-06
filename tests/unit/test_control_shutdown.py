import asyncio
import os
import signal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.types import Message, Receive, Scope, Send

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.control import _shutdown_current_process, control_router


@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
async def test_committed_shutdown_survives_response_loss_and_request_cancellation(
    failure: str,
) -> None:
    secret, operation = "a" * 64, "b" * 64
    handler = WebhookHandler(secret="webhook", github=AsyncMock(), use_case=AsyncMock())
    app = FastAPI()
    app.state.handler = handler
    shutdown_requested, sending = asyncio.Event(), asyncio.Event()
    shutdown = Mock(side_effect=shutdown_requested.set)
    app.include_router(control_router(
        Settings.model_construct(control_shared_secret=SecretStr(secret)),
        request_shutdown=shutdown,
    ))

    async def broken_response(scope: Scope, receive: Receive, send: Send) -> None:
        async def send_or_fail(message: Message) -> None:
            if scope["path"].endswith("commit-restart"):
                sending.set()
                if failure == "cancel":
                    await asyncio.Event().wait()
                raise ConnectionError("response lost after server committed")
            await send(message)

        await app(scope, receive, send_or_fail)

    transport = httpx.ASGITransport(app=broken_response, client=("127.0.0.1", 1234))
    headers = {"X-Gorani-Bot-Control-Secret": secret}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            drained = await client.post("/internal/control/drain", headers=headers,
                                        json={"operationId": operation})
            request = asyncio.create_task(client.post(
                "/internal/control/commit-restart", headers=headers,
                json={"operationId": operation, "instanceId": drained.json()["instanceId"]},
            ))
            await asyncio.wait_for(sending.wait(), 1)
            if failure == "cancel":
                request.cancel()
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else ConnectionError):
                await request
            await asyncio.wait_for(shutdown_requested.wait(), 1)
            shutdown.assert_called_once_with()
            assert not handler.intake.resume(operation)
            await handler.intake._expire(operation, 0)
            assert handler.intake.restart_committed
            assert handler.intake.operation_id == operation
    finally:
        await handler.stop()


def test_shutdown_only_signals_own_process(monkeypatch: pytest.MonkeyPatch) -> None:
    kill = Mock()
    monkeypatch.setattr("codex_review.control.os.kill", kill)
    _shutdown_current_process()
    kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
