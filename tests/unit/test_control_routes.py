import os
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.control import control_router

SECRET = "a" * 64
OPERATION = "b" * 64
HEADERS = {"X-Gorani-Bot-Control-Secret": SECRET}


def test_control_secret_cannot_reuse_webhook_secret() -> None:
    with pytest.raises(ValueError, match="must differ"):
        Settings(
            GITHUB_APP_ID=123,
            GITHUB_APP_PRIVATE_KEY="placeholder",
            GITHUB_WEBHOOK_SECRET=SECRET,
            CODEX_CONTROL_SHARED_SECRET=SECRET,
        )


def application(enabled: bool = True) -> FastAPI:
    settings = Settings.model_construct(
        control_shared_secret=SecretStr(SECRET) if enabled else None,
        codex_model="configured-model", codex_reasoning_effort="high",
        codex_fallback_models="fallback-one,fallback-two",
    )
    app = FastAPI()
    app.state.handler = WebhookHandler(
        secret="webhook", github=AsyncMock(), use_case=AsyncMock(),
    )
    app.include_router(control_router(settings))
    return app


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
async def test_status_reports_configured_state_and_stable_instance_identity(peer: str) -> None:
    app = application()
    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/internal/control/status", headers=HEADERS)
        second = await client.get("/internal/control/status", headers=HEADERS)
    assert first.status_code == 200
    body = first.json()
    assert body == second.json()
    assert body["pid"] == os.getpid()
    assert body["model"] == "configured-model"
    assert body["reasoningEffort"] == "high"
    assert body["fallbacks"] == ["fallback-one", "fallback-two"]
    assert len(body["instanceId"]) == 32
    assert body["queueDepth"] == body["activeJobs"] == 0
    assert body["draining"] is False
    assert SECRET not in first.text


@pytest.mark.parametrize("path", ["status", "drain", "resume"])
@pytest.mark.parametrize("peer,secret,enabled", [
    ("192.0.2.1", SECRET, True),
    ("unresolved-peer", SECRET, True),
    ("127.0.0.1", "wrong", True),
    ("127.0.0.1", "", True),
    ("127.0.0.1", SECRET, False),
])
async def test_control_rejects_unauthorized_peers_even_with_spoofed_forwarding(
    path: str, peer: str, secret: str, enabled: bool,
) -> None:
    app = application(enabled)
    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            "GET" if path == "status" else "POST", f"/internal/control/{path}",
            headers={"X-Gorani-Bot-Control-Secret": secret, "X-Forwarded-For": "127.0.0.1"},
            json={"operationId": OPERATION},
        )
    assert response.status_code == 403
    assert app.state.handler.intake.operation_id is None


async def test_drain_and_resume_require_matching_operation_owner() -> None:
    app = application()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            drained = await client.post(
                "/internal/control/drain", headers=HEADERS, json={"operationId": OPERATION},
            )
            assert drained.status_code == 200
            assert drained.json()["status"] == "drained"
            conflict = await client.post(
                "/internal/control/drain", headers=HEADERS, json={"operationId": "c" * 64},
            )
            assert conflict.status_code == 409
            wrong = await client.post(
                "/internal/control/resume", headers=HEADERS, json={"operationId": "c" * 64},
            )
            assert wrong.json() == {"resumed": False}
            status = await client.get("/internal/control/status", headers=HEADERS)
            assert status.json()["operationId"] == OPERATION
            assert status.json()["instanceId"] == drained.json()["instanceId"]
            resumed = await client.post(
                "/internal/control/resume", headers=HEADERS, json={"operationId": OPERATION},
            )
            assert resumed.json() == {"resumed": True}
    finally:
        await app.state.handler.stop()


@pytest.mark.parametrize("body", [
    {}, {"operationId": "short"}, {"operationId": "A" * 64},
    {"operationId": OPERATION, "command": "restart"},
])
async def test_control_rejects_malformed_operation_without_pausing(body: dict) -> None:
    app = application()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/internal/control/drain", headers=HEADERS, json=body)
    assert response.status_code == 422
    assert app.state.handler.intake.operation_id is None


async def test_drain_timeout_is_reported_as_retryable_conflict() -> None:
    app = application()
    app.state.handler.drain = AsyncMock(side_effect=TimeoutError)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/control/drain", headers=HEADERS, json={"operationId": OPERATION},
        )
    assert response.status_code == 409
    assert "intake resumed" in response.json()["detail"]
