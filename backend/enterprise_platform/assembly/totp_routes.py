"""账户自助 TOTP 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from enterprise_platform.assembly.contracts import AUTH_TOTP_ADVANCE, AUTH_TOTP_CREATE
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.schemas import (
    CurrentUser,
    TotpBeginResponse,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpStatusResponse,
)


def register_totp_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_totp_status(router, ctx)
    _register_totp_begin(router, ctx)
    _register_totp_confirm(router, ctx)
    _register_totp_disable(router, ctx)


def _register_totp_status(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/users/me/totp/status", response_model=TotpStatusResponse, tags=["auth"])
    def totp_status(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_TOTP_ADVANCE)),
    ) -> TotpStatusResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "totp_status")
        return TotpStatusResponse(enabled=ctx.port_call(lambda: ctx.ports.account.totp_status(user.id)))


def _register_totp_begin(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/users/me/totp/begin", response_model=TotpBeginResponse, tags=["auth"])
    def totp_begin(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_TOTP_CREATE)),
    ) -> TotpBeginResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "totp_begin")
        secret, uri = ctx.port_call(lambda: ctx.ports.account.totp_begin(user.id))
        return TotpBeginResponse(secret=secret, otpauth_uri=uri)


def _register_totp_confirm(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/users/me/totp/confirm", response_model=TotpStatusResponse, tags=["auth"])
    def totp_confirm(
        body: TotpConfirmRequest,
        request: Request,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_TOTP_CREATE)),
    ) -> TotpStatusResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "totp_confirm")
        ctx.run_reverified_mutation(
            request,
            user.id,
            "totp_confirm",
            lambda: ctx.ports.account.totp_confirm(user.id, body.code),
            "验证码错误",
        )
        ctx.hooks.after_event(
            user.id, "auth.totp.enable", {"enabled": False}, {"enabled": True, "sessionsRevoked": True}
        )
        return TotpStatusResponse(enabled=True)


def _register_totp_disable(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/users/me/totp/disable", response_model=TotpStatusResponse, tags=["auth"])
    def totp_disable(
        body: TotpDisableRequest,
        request: Request,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_TOTP_ADVANCE)),
    ) -> TotpStatusResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "totp_disable")
        ctx.run_reverified_mutation(
            request,
            user.id,
            "totp_disable",
            lambda: ctx.ports.account.totp_disable(user.id, body.password, body.code),
            "密码或验证码错误",
        )
        ctx.hooks.after_event(
            user.id, "auth.totp.disable", {"enabled": True}, {"enabled": False, "sessionsRevoked": True}
        )
        return TotpStatusResponse(enabled=False)
