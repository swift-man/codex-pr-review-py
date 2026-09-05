"""Authenticated loopback-only restart coordination; never executes commands."""

import hmac
import ipaddress
import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings


class ControlOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(alias="operationId", pattern=r"^[0-9a-f]{64}$")


def control_router(settings: Settings) -> APIRouter:
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
            "draining": current.intake.operation_id is not None,
            "operationId": current.intake.operation_id,
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

    return router
