"""Authenticated loopback-only restart coordination; never executes commands."""

import asyncio
import hmac
import ipaddress
import os
import signal
import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.model_utils import effective_reasoning_effort


class ControlOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(alias="operationId", pattern=r"^[0-9a-f]{64}$")


class RestartOperation(ControlOperation):
    instance_id: str = Field(alias="instanceId", pattern=r"^[0-9a-f]{32}$")


def _shutdown_current_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def control_router(
    settings: Settings, *, request_shutdown: Callable[[], None] = _shutdown_current_process,
) -> APIRouter:
    router = APIRouter(prefix="/internal/control")
    instance_id = uuid.uuid4().hex

    def authorize(request: Request, response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        secret = settings.control_shared_secret
        try:
            peer_is_loopback = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            peer_is_loopback = False
        supplied = request.headers.get("X-Gorani-Bot-Control-Secret", "")
        if (
            not peer_is_loopback
            or secret is None
            or not hmac.compare_digest(supplied.encode(), secret.get_secret_value().encode())
        ):
            raise HTTPException(403, "Forbidden")

    def handler(request: Request) -> WebhookHandler:
        result: WebhookHandler = request.app.state.handler
        return result

    dependency = [Depends(authorize)]

    @router.get("/status", dependencies=dependency)
    async def status(current: Annotated[WebhookHandler, Depends(handler)]) -> dict[str, object]:
        return {
            "instanceId": instance_id,
            "pid": os.getpid(),
            "model": settings.codex_model,
            "reasoningEffort": settings.codex_reasoning_effort,
            "fallbacks": list(settings.codex_model_fallbacks),
            "fallbackReasoningEffort": settings.codex_fallback_reasoning_effort,
            "fallbackReasoningEfforts": {
                model: effective_reasoning_effort(
                    model,
                    settings.codex_fallback_reasoning_effort or settings.codex_reasoning_effort,
                )
                for model in settings.codex_model_fallbacks if model != settings.codex_model
            },
            "draining": current.intake.operation_id is not None,
            "operationId": current.intake.operation_id,
            "restartCommitted": current.intake.restart_committed,
            "queueDepth": current.queue_depth,
            "activeJobs": current.intake.active_jobs,
        }

    @router.post("/drain", dependencies=dependency)
    async def drain(
        operation: ControlOperation,
        current: Annotated[WebhookHandler, Depends(handler)],
    ) -> dict[str, object]:
        try:
            drained = await current.drain(operation.operation_id)
        except TimeoutError as exc:
            raise HTTPException(409, "Drain timed out; intake resumed") from exc
        if not drained:
            raise HTTPException(409, "Another control operation is active")
        return {
            "status": "drained",
            "instanceId": instance_id,
            "operationId": operation.operation_id,
            "queueDepth": current.queue_depth,
            "activeJobs": current.intake.active_jobs,
        }

    @router.post("/resume", dependencies=dependency)
    async def resume(
        operation: ControlOperation,
        current: Annotated[WebhookHandler, Depends(handler)],
    ) -> dict[str, object]:
        return {"resumed": current.intake.resume(operation.operation_id)}

    @router.post("/commit-restart", dependencies=dependency)
    async def commit_restart(
        operation: RestartOperation,
        current: Annotated[WebhookHandler, Depends(handler)],
    ) -> dict[str, object]:
        already_committed = current.intake.restart_committed
        if (
            operation.instance_id != instance_id
            or not current.commit_restart(operation.operation_id)
        ):
            raise HTTPException(409, "Restart handoff no longer matches an idle drain")
        if not already_committed:
            # Independent of response delivery: a disconnected launcher cannot
            # leave this process alive with intake permanently sealed.
            asyncio.get_running_loop().call_soon(request_shutdown)
        return {
            "status": "restart-committed", "instanceId": instance_id,
            "operationId": operation.operation_id, "pid": os.getpid(),
        }

    return router
