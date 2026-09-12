"""The authenticated runtime snapshot distinguishes saved and effective fallback effort."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.control import control_router
from codex_review.model_utils import ReasoningEffort


@pytest.mark.parametrize("override,expected", [(None, "xhigh"), ("max", "max")])
@pytest.mark.parametrize("fallbacks", [
    "gpt-6-astra,gpt-5.6-luna,gpt-reserve,gpt-5.3-codex-spark,gpt-reserve",
    "",
])
async def test_status_projects_saved_override_and_effective_fallback_efforts(
    override: ReasoningEffort | None, expected: str, fallbacks: str,
) -> None:
    secret = "a" * 64
    settings = Settings.model_construct(
        control_shared_secret=SecretStr(secret),
        codex_model="gpt-6-astra", codex_reasoning_effort="xhigh",
        codex_fallback_reasoning_effort=override, codex_fallback_models=fallbacks,
    )
    app = FastAPI()
    app.state.handler = WebhookHandler(
        secret="webhook", github=AsyncMock(), use_case=AsyncMock(),
    )
    app.include_router(control_router(settings))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/internal/control/status", headers={"X-Gorani-Bot-Control-Secret": secret},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reasoningEffort"] == "xhigh"
    assert body["fallbackReasoningEffort"] == override
    assert body["fallbackReasoningEfforts"] == ({
        "gpt-5.6-luna": expected, "gpt-reserve": expected,
        "gpt-5.3-codex-spark": "xhigh",
    } if fallbacks else {})
    assert secret not in response.text
