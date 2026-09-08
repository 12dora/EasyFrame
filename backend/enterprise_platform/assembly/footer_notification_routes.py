"""通用设置与通知中心路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from enterprise_platform.assembly.contracts import NOTIFICATION_CENTER_VIEW, SETTINGS_UPDATE
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.footer import clean_plain_text, sanitize_footer_html, validate_logo_data_url
from enterprise_platform.schemas import (
    CurrentUser,
    FooterSettings,
    FooterSettingsUpdate,
    GeneralSettings,
    GeneralSettingsUpdate,
    NotificationPage,
)

_TITLE_MAX = 80
_SUBTITLE_MAX = 200


def register_footer_notification_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_get_general(router, ctx)
    _register_put_general(router, ctx)
    _register_get_footer(router, ctx)
    _register_put_footer(router, ctx)
    _register_list_notifications(router, ctx)
    _register_mark_read(router, ctx)
    _register_mark_all_read(router, ctx)


def _register_get_general(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/app-settings/general", response_model=GeneralSettings, tags=["app-settings"])
    def get_general() -> GeneralSettings:
        return ctx.port_call(ctx.ports.app_settings.get_general)


def _general_from_update(body: GeneralSettingsUpdate) -> GeneralSettings:
    return GeneralSettings(
        title_zh=clean_plain_text(body.title_zh, max_length=_TITLE_MAX),
        title_en=clean_plain_text(body.title_en, max_length=_TITLE_MAX),
        subtitle_zh=clean_plain_text(body.subtitle_zh, max_length=_SUBTITLE_MAX),
        subtitle_en=clean_plain_text(body.subtitle_en, max_length=_SUBTITLE_MAX),
        footer_html_zh=sanitize_footer_html(body.footer_html_zh),
        footer_html_en=sanitize_footer_html(body.footer_html_en),
        logo_data_url=validate_logo_data_url(body.logo_data_url),
    )


def _register_put_general(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/app-settings/general", response_model=GeneralSettings, tags=["app-settings"])
    def put_general(
        body: GeneralSettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(SETTINGS_UPDATE)),
    ) -> GeneralSettings:
        try:
            settings = _general_from_update(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ctx.port_call(lambda: ctx.ports.app_settings.save_general(settings, actor_id=user.id))


def _footer_from_general(settings: GeneralSettings) -> FooterSettings:
    return FooterSettings(footer_html_zh=settings.footer_html_zh, footer_html_en=settings.footer_html_en)


def _register_get_footer(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def get_footer() -> FooterSettings:
        return _footer_from_general(ctx.port_call(ctx.ports.app_settings.get_general))


def _register_put_footer(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def put_footer(
        body: FooterSettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(SETTINGS_UPDATE)),
    ) -> FooterSettings:
        current = ctx.port_call(ctx.ports.app_settings.get_general)
        updated = current.model_copy(
            update={
                "footer_html_zh": sanitize_footer_html(body.footer_html_zh),
                "footer_html_en": sanitize_footer_html(body.footer_html_en),
            }
        )
        saved = ctx.port_call(lambda: ctx.ports.app_settings.save_general(updated, actor_id=user.id))
        return _footer_from_general(saved)


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
