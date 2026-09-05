"""账户自助 passkey 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from enterprise_platform.assembly.contracts import AUTH_PASSKEY_CREATE, AUTH_PASSKEY_VIEW
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.schemas import (
    CurrentUser,
    PasskeyRegisterBeginResponse,
    PasskeyRegisterCompleteRequest,
    PasskeyRegisterCompleteResponse,
    PasskeySummary,
)


def register_passkey_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_list_passkeys(router, ctx)
    _register_passkey_begin(router, ctx)
    _register_passkey_complete(router, ctx)
    _register_delete_passkey(router, ctx)


def _register_list_passkeys(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/users/me/passkeys", response_model=list[PasskeySummary], tags=["auth"])
    def list_passkeys(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_PASSKEY_VIEW)),
    ) -> list[PasskeySummary]:
        ctx.hooks.ensure_local_auth_management_allowed(user, "passkey_list")
        return ctx.port_call(lambda: ctx.ports.account.list_passkeys(user.id))


def _register_passkey_begin(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/users/me/passkeys/register/begin", response_model=PasskeyRegisterBeginResponse, tags=["auth"])
    def passkey_register_begin(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_PASSKEY_CREATE)),
    ) -> PasskeyRegisterBeginResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "passkey_register_begin")
        challenge = ctx.port_call(lambda: ctx.ports.account.begin_passkey_registration(user.id))
        return PasskeyRegisterBeginResponse(options=challenge.options, state_token=challenge.state_token)


def _register_passkey_complete(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post(
        "/users/me/passkeys/register/complete",
        response_model=PasskeyRegisterCompleteResponse,
        status_code=201,
        tags=["auth"],
    )
    def passkey_register_complete(
        body: PasskeyRegisterCompleteRequest,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_PASSKEY_CREATE)),
    ) -> PasskeyRegisterCompleteResponse:
        ctx.hooks.ensure_local_auth_management_allowed(user, "passkey_register_complete")
        item = ctx.port_call(
            lambda: ctx.ports.account.complete_passkey_registration(
                user.id, body.state_token, body.credential, body.name.strip()
            )
        )
        ctx.hooks.after_event(user.id, "auth.passkey.create", None, {"passkeyId": item.id, "sessionsRevoked": True})
        return PasskeyRegisterCompleteResponse(id=item.id, name=item.name)


def _register_delete_passkey(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.delete("/users/me/passkeys/{passkey_id}", status_code=204, tags=["auth"])
    def delete_passkey(
        passkey_id: str,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTH_PASSKEY_CREATE)),
    ) -> Response:
        ctx.hooks.ensure_local_auth_management_allowed(user, "passkey_delete")
        if not ctx.port_call(lambda: ctx.ports.account.delete_passkey(user.id, passkey_id)):
            raise HTTPException(404, "通行密钥不存在")
        ctx.hooks.after_event(user.id, "auth.passkey.delete", {"passkeyId": passkey_id}, {"sessionsRevoked": True})
        return Response(status_code=204)
