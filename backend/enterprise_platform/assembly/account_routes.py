"""账户自助:当前用户、登出、改密,并编排 TOTP / passkey。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.assembly.passkey_routes import register_passkey_routes
from enterprise_platform.assembly.totp_routes import register_totp_routes
from enterprise_platform.schemas import ChangePasswordRequest, CurrentUser


def register_account_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_me(router, ctx)
    _register_logout(router, ctx)
    _register_change_password(router, ctx)
    register_totp_routes(router, ctx)
    register_passkey_routes(router, ctx)


def _register_me(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/auth/me", response_model=CurrentUser, tags=["auth"])
    def me(user: CurrentUser = Depends(ctx.recovery_user)) -> CurrentUser:
        return ctx.hooks.present_current_user(user)


def _register_logout(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/auth/logout", tags=["auth"])
    def logout(user: CurrentUser = Depends(ctx.recovery_user)) -> dict:
        outcome = ctx.port_call(lambda: ctx.ports.account.revoke_sessions(user.id))
        if isinstance(outcome, dict):
            ctx.hooks.after_event(user.id, "auth.logout", None, _logout_audit(outcome))
            return outcome
        ctx.hooks.after_event(user.id, "auth.logout", None, {"sessionsRevoked": True})
        return {"ok": True}


def _logout_audit(outcome: dict) -> dict[str, bool]:
    return {
        "localSessionsRevoked": outcome.get("localSessionsRevoked", True),
        "gatewayLogoutRequired": outcome.get("gatewayLogoutRequired", False),
    }


def _register_change_password(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/users/me/password", tags=["auth"])
    def change_password(
        body: ChangePasswordRequest, request: Request, user: CurrentUser = Depends(ctx.recovery_user)
    ) -> dict[str, bool]:
        ctx.hooks.ensure_local_auth_management_allowed(user, "change_password")
        # 「新旧口令相同」是入参校验、不是凭据失败,必须在额度之外先拒掉。
        if body.current_password == body.new_password:
            raise HTTPException(422, "新密码不能与当前密码相同")
        # 改密要校验 currentPassword,是一次真实的口令哈希验证,因此和 TOTP confirm/disable
        # 共用同一份「已登录主体再验证」额度(同一 subject、同一计数键),而不是新开一个桶:
        # 新开桶等于把持有会话的攻击者可强制执行的 argon2/bcrypt 次数翻倍,
        # 那既是猜口令的 oracle,也是 CPU 消耗向量。
        ctx.run_reverified_mutation(
            request,
            user.id,
            "change_password",
            lambda: ctx.ports.account.change_password(user.id, body.current_password, body.new_password),
            "当前密码错误",
        )
        ctx.hooks.after_event(user.id, "auth.password.change", None, {"sessionsRevoked": True})
        return {"ok": True}
