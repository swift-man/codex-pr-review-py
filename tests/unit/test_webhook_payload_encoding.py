"""Signed malformed payloads fail at the HTTP boundary before job acceptance."""

import hashlib
import hmac
from unittest.mock import AsyncMock

import httpx
import pytest

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.main import create_app


@pytest.mark.parametrize("body", [b'\xff', b'{"value":"\xff"}', b'{broken'])
async def test_signed_invalid_encoding_or_json_returns_400(body: bytes) -> None:
    settings = Settings(
        GITHUB_APP_ID=123, GITHUB_APP_PRIVATE_KEY="placeholder",
        GITHUB_WEBHOOK_SECRET="test-webhook-secret", _env_file=None,
    )
    app = create_app(settings)
    handler = WebhookHandler(secret="test-webhook-secret", github=AsyncMock(), use_case=AsyncMock())
    handler.accept = AsyncMock()
    app.state.handler = handler
    signature = "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        unsigned = await client.post("/webhook", content=body)
        signed = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": signature},
        )
    assert unsigned.status_code == 401
    assert signed.status_code == 400
    assert signed.text == "invalid json"
    handler.accept.assert_not_awaited()
