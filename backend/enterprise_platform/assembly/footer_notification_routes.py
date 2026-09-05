"""页脚与通知中心路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from enterprise_platform.assembly.contracts import NOTIFICATION_CENTER_VIEW, SETTINGS_UPDATE
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.footer import sanitize_footer_html
from enterprise_platform.schemas import CurrentUser, FooterSettings, FooterSettingsUpdate, NotificationPage


def register_footer_notification_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_get_footer(router, ctx)
    _register_put_footer(router, ctx)
    _register_list_notifications(router, ctx)
    _register_mark_read(router, ctx)
    _register_mark_all_read(router, ctx)


def _register_get_footer(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def get_footer() -> FooterSettings:
        return ctx.port_call(ctx.ports.footer.get_footer)


def _register_put_footer(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def put_footer(
        body: FooterSettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(SETTINGS_UPDATE)),
    ) -> FooterSettings:
        footer = FooterSettings(
            footer_html_zh=sanitize_footer_html(body.footer_html_zh),
            footer_html_en=sanitize_footer_html(body.footer_html_en),
        )
        return ctx.port_call(lambda: ctx.ports.footer.save_footer(footer, actor_id=user.id))


def _register_list_notifications(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/notifications", response_model=NotificationPage, tags=["notifications"])
    def notifications(
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> NotificationPage:
        return ctx.port_call(lambda: ctx.ports.notifications.list_notifications(user.id, cursor=cursor, limit=limit))


def _register_mark_read(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/notifications/{notification_id}/read", tags=["notifications"])
    def mark_read(
        notification_id: str,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> dict[str, bool]:
        if not ctx.port_call(
            lambda: ctx.ports.notifications.mark_read(user.id, notification_id, read_at=datetime.now(UTC))
        ):
            raise HTTPException(404, "通知不存在")
        return {"ok": True}


def _register_mark_all_read(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/notifications/read-all", tags=["notifications"])
    def mark_all_read(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> dict[str, int]:
        return {
            "updated": ctx.port_call(lambda: ctx.ports.notifications.mark_all_read(user.id, read_at=datetime.now(UTC)))
        }
