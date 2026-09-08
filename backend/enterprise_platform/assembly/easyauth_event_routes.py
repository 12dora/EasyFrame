"""EasyAuth 入站事件:验签后按 event_type 分发。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.easyauth.webhook import (
    CATALOG_CHANGED_EVENT,
    GRANT_CHANGED_EVENT,
    WEBHOOK_TEST_EVENT,
    WebhookEvent,
    WebhookVerificationError,
    verify_webhook,
)


def register_easyauth_event_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/easyauth/events", tags=["easyauth"])
    async def easyauth_events(request: Request) -> JSONResponse:
        raw_body = await request.body()
        try:
            event = verify_webhook(
                dict(request.headers),
                raw_body,
                _webhook_secret(ctx),
                datetime.now(UTC),
            )
        except WebhookVerificationError:
            return JSONResponse(status_code=401, content={"error": "invalid_signature"})
        return _dispatch_event(ctx, event)


def _webhook_secret(ctx: AssemblyDependencies) -> str:
    try:
        return str(ctx.port_call(ctx.ports.integrations.get_easyauth_webhook_secret) or "")
    except (AttributeError, HTTPException):
        return ""


def _dispatch_event(ctx: AssemblyDependencies, event: WebhookEvent) -> JSONResponse:
    if event.event_type == WEBHOOK_TEST_EVENT:
        return JSONResponse({"ok": True})
    if event.event_type == GRANT_CHANGED_EVENT:
        return _handle_grant_changed(ctx, event.payload)
    if event.event_type == CATALOG_CHANGED_EVENT:
        return _handle_catalog_changed(ctx, event.payload)
    return JSONResponse(status_code=422, content={"error": "unsupported_event"})


def _handle_grant_changed(ctx: AssemblyDependencies, payload: dict[str, Any]) -> JSONResponse:
    user_id = payload.get("user_id")
    snapshot_version = payload.get("snapshot_version")
    if not isinstance(user_id, str) or not user_id or not isinstance(snapshot_version, str):
        return JSONResponse(status_code=422, content={"error": "invalid_payload"})
    ops = ctx.ports.authorization
    if ops is None:
        raise HTTPException(503, "authorization port is not configured")
    ctx.port_call(lambda: ops.refresh_snapshot_for_external_user(user_id, snapshot_version))
    return JSONResponse({"ok": True})


def _handle_catalog_changed(ctx: AssemblyDependencies, payload: dict[str, Any]) -> JSONResponse:
    app_key = payload.get("app_key")
    if not isinstance(app_key, str) or not app_key:
        return JSONResponse(status_code=422, content={"error": "invalid_payload"})
    ops = ctx.ports.authorization
    if ops is None:
        raise HTTPException(503, "authorization port is not configured")
    ctx.port_call(lambda: ops.invalidate_app_snapshots(app_key))
    return JSONResponse({"ok": True})
