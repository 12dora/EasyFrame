"""登录路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.login_flow import (
    _run_passkey_login_begin,
    _run_passkey_login_complete,
    _run_password_login,
)
from enterprise_platform.schemas import (
    LoginRequest,
    LoginResponse,
    PasskeyLoginBeginRequest,
    PasskeyLoginBeginResponse,
    PasskeyLoginCompleteRequest,
)


def register_login_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_password_login(router, ctx)
    _register_passkey_login_begin(router, ctx)
    _register_passkey_login_complete(router, ctx)


def _register_password_login(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
    def login(body: LoginRequest, request: Request) -> LoginResponse:
        return _run_password_login(
            body,
            request,
            account=ctx.ports.account,
            login_guard=ctx.login_guard,
            admit_second_factor=ctx.admit_second_factor,
            login_event=ctx.hooks.login_event,
        )


def _register_passkey_login_begin(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/auth/login/passkey/begin", response_model=PasskeyLoginBeginResponse, tags=["auth"])
    def passkey_login_begin(body: PasskeyLoginBeginRequest, request: Request) -> PasskeyLoginBeginResponse:
        return _run_passkey_login_begin(
            body,
            request,
            account=ctx.ports.account,
            login_guard=ctx.login_guard,
            login_event=ctx.hooks.login_event,
        )


def _register_passkey_login_complete(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/auth/login/passkey/complete", response_model=LoginResponse, tags=["auth"])
    def passkey_login_complete(body: PasskeyLoginCompleteRequest, request: Request) -> LoginResponse:
        return _run_passkey_login_complete(
            body,
            request,
            account=ctx.ports.account,
            login_guard=ctx.login_guard,
            admit_second_factor=ctx.admit_second_factor,
            login_event=ctx.hooks.login_event,
        )
