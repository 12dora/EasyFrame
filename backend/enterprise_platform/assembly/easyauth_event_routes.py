"""EasyAuth 入站事件:验签后按 event_type 分发。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

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
        return await run_in_threadpool(_dispatch_event, ctx, event)


def _webhook_secret(ctx: AssemblyDependencies) -> str:
    try:
        return str(ctx.port_call(ctx.ports.integrations.get_easyauth_webhook_secret) or "")
    except (AttributeError, HTTPException):
        return ""


def _dispatch_event(ctx: AssemblyDependencies, event: WebhookEvent) -> JSONResponse:
    if event.event_type == WEBHOOK_TEST_EVENT:
        return JSONResponse({"ok": True})
    if event.event_type == GRANT_CHANGED_EVENT:
        rejected = _reject_unmatched_app(ctx, event.payload)
        if rejected is not None:
            return rejected
        return _handle_grant_changed(ctx, event.payload)
    if event.event_type == CATALOG_CHANGED_EVENT:
        rejected = _reject_unmatched_app(ctx, event.payload)
        if rejected is not None:
            return rejected
        return _handle_catalog_changed(ctx, event.payload)
    return JSONResponse(status_code=422, content={"error": "unsupported_event"})


def _reject_unmatched_app(ctx: AssemblyDependencies, payload: dict[str, Any]) -> JSONResponse | None:
    app_key = payload.get("app_key")
    if not isinstance(app_key, str) or not app_key:
        return JSONResponse(status_code=422, content={"error": "invalid_payload"})
    configured = _configured_app_key(ctx)
    if app_key != configured:
        return JSONResponse(status_code=422, content={"error": "app_key_mismatch"})
    return None


def _configured_app_key(ctx: AssemblyDependencies) -> str:
    getter = getattr(ctx.ports.integrations, "get_easyauth_status", None)
    if not callable(getter):
        return ""
    try:
        status = ctx.port_call(getter)
    except HTTPException:
        return ""
    return str(getattr(status, "app_key", "") or "")


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
    catalog_version = payload.get("catalog_version")
    if not isinstance(catalog_version, int) or catalog_version < 1:
        return JSONResponse(status_code=422, content={"error": "invalid_payload"})
    app_key = payload.get("app_key")
    if not isinstance(app_key, str) or not app_key:
        return JSONResponse(status_code=422, content={"error": "invalid_payload"})
    ops = ctx.ports.authorization
    if ops is None:
        raise HTTPException(503, "authorization port is not configured")
    ctx.port_call(lambda: ops.invalidate_app_snapshots(app_key, catalog_version))
    return JSONResponse({"ok": True})
