"""共享 FastAPI router factory。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from enterprise_platform.auth import AuthError
from enterprise_platform.auth import authenticate_login as authenticate_login
from enterprise_platform.auth import begin_passkey_login as begin_passkey_login
from enterprise_platform.auth import complete_passkey_login as complete_passkey_login
from enterprise_platform.auth import is_credential_failure as is_credential_failure
from enterprise_platform.footer import sanitize_footer_html
from enterprise_platform.login_flow import (
    LoginAdmissionGuard,
    _run_passkey_login_begin,
    _run_passkey_login_complete,
    _run_password_login,
)
from enterprise_platform.ports import (
    AccountPort,
    DirectoryPort,
    FooterPort,
    IntegrationPort,
    NotificationPort,
    PermissionCheck,
    UpstreamHealthPort,
)
from enterprise_platform.rate_limit import (
    clear_second_factor_failures,
    enforce_login,
    enforce_second_factor,
    record_second_factor_failure,
    second_factor_failure_admission,
)
from enterprise_platform.response_redaction import (
    has_permission,
    present_authorization_settings,
    present_directory_settings,
    present_oidc_settings,
)
from enterprise_platform.schemas import (
    AuthorizationSettingsSummary,
    ChangePasswordRequest,
    ConnectionTestResult,
    CurrentUser,
    DirectorySettings,
    DirectorySettingsSummary,
    DirectorySettingsUpdate,
    DirectorySyncResult,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    FooterSettings,
    FooterSettingsUpdate,
    IdentityDiscoveryRequest,
    IdentityDiscoveryResponse,
    LoginRequest,
    LoginResponse,
    NotificationPage,
    OidcSettings,
    OidcSettingsSummary,
    OidcSettingsUpdate,
    PasskeyLoginBeginRequest,
    PasskeyLoginBeginResponse,
    PasskeyLoginCompleteRequest,
    PasskeyRegisterBeginResponse,
    PasskeyRegisterCompleteRequest,
    PasskeyRegisterCompleteResponse,
    PasskeySummary,
    TotpBeginResponse,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpStatusResponse,
    UpstreamHealthItem,
)

AUTH_TOTP_CREATE = "auth.totp.create"
AUTH_TOTP_ADVANCE = "auth.totp.advance"
AUTH_PASSKEY_VIEW = "auth.passkey.view"
AUTH_PASSKEY_CREATE = "auth.passkey.create"
SETTINGS_UPDATE = "settings.app_setting.update"
IDENTITY_VIEW = "identity.integration.view"
IDENTITY_MANAGE = "identity.integration.manage"
AUTHZ_VIEW = "authz.integration.view"
AUTHZ_MANAGE = "authz.integration.manage"
UPSTREAM_VIEW = "ops.upstream_health.view"
UPSTREAM_MANAGE = "ops.upstream_health.manage"
NOTIFICATION_CENTER_VIEW = "notification.center.view"


@dataclass(frozen=True)
class PlatformPorts:
    account: AccountPort
    footer: FooterPort
    notifications: NotificationPort
    integrations: IntegrationPort
    directory: DirectoryPort
    upstream_health: UpstreamHealthPort
    require_permission: PermissionCheck


def _noop_before_password_login(_username: str, _request: Request) -> None:
    return None


def _noop_before_second_factor(_account_id: str, _operation: str, _request: Request) -> None:
    return None


def _noop_local_auth_policy(_user: CurrentUser, _operation: str) -> None:
    return None


def _noop_after_event(
    _actor_id: str, _action: str, _before: dict[str, Any] | None, _after: dict[str, Any] | None
) -> None:
    return None


def _noop_login_event(_actor_id: str, _action: str, _method: str) -> None:
    return None


def _identity_presenter(user: CurrentUser) -> CurrentUser:
    return user


@dataclass(frozen=True)
class PlatformSecurityHooks:
    """宿主可注入的认证策略与审计 hooks；共享层不依赖宿主 ORM。"""

    before_password_login: Callable[[str, Request], None] = _noop_before_password_login
    before_second_factor: Callable[[str, str, Request], None] = _noop_before_second_factor
    ensure_local_auth_management_allowed: Callable[[CurrentUser, str], None] = _noop_local_auth_policy
    after_event: Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], None] = _noop_after_event
    login_event: Callable[[str, str, str], None] = _noop_login_event
    present_current_user: Callable[[CurrentUser], CurrentUser] = _identity_presenter


@dataclass(frozen=True)
class PlatformRouteGroups:
    auth: bool = True
    # None(缺省)= 跟随 auth,保持旧语义;宿主自有登录但复用框架登出时显式传 True。
    logout: bool | None = None
    me: bool = True
    password: bool = True
    totp: bool = True
    passkeys: bool = True
    footer: bool = True
    notifications: bool = True
    identity: bool = True
    easyauth: bool = True
    upstream: bool = True


def create_platform_router(
    ports: PlatformPorts,
    *,
    include_authz_integration: bool = True,
    route_groups: PlatformRouteGroups | None = None,
    security_hooks: PlatformSecurityHooks | None = None,
    current_user_dependency: Callable[..., CurrentUser] | None = None,
    permission_dependency_factory: Callable[[str], Callable[..., Any]] | None = None,
    enforce_shared_rate_limits: bool = True,
) -> APIRouter:
    """创建完整企业框架 API；宿主只负责适配 ports。"""

    router = APIRouter()
    groups = route_groups or PlatformRouteGroups()
    hooks = security_hooks or PlatformSecurityHooks()

    def fixed_current_user() -> CurrentUser:
        try:
            return ports.account.current_user()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    resolved_current_user = current_user_dependency or fixed_current_user

    def recovery_user(user: CurrentUser = Depends(resolved_current_user)) -> CurrentUser:
        return user

    def current_user(user: CurrentUser = Depends(recovery_user)) -> CurrentUser:
        if user.must_change_password:
            raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
        return user

    def permission_for(code: str) -> Callable[..., Any]:
        if permission_dependency_factory is not None:
            return permission_dependency_factory(code)

        def fixed_permission(request: Request) -> None:
            try:
                # 透传 Request,便于拒绝审计记录 method/route。
                ports.require_permission(code, request)  # type: ignore[call-arg]
            except TypeError:
                try:
                    ports.require_permission(code)
                except AuthError as exc:
                    raise HTTPException(exc.status_code, exc.detail) from exc
            except AuthError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc

        return fixed_permission

    def port_call(callback: Callable[[], Any]) -> Any:
        try:
            return callback()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def audited_connection_test(
        callback: Callable[[], ConnectionTestResult], *, actor_id: str, action: str
    ) -> ConnectionTestResult:
        try:
            result = port_call(callback)
        except HTTPException:
            hooks.after_event(actor_id, action, None, {"ok": False, "latencyMs": 0, "errorKind": "request_failed"})
            raise
        hooks.after_event(
            actor_id,
            action,
            None,
            {"ok": result.ok, "latencyMs": result.latency_ms, "errorKind": result.error_kind},
        )
        return result

    def admit_login_attempt(username: str, request: Request) -> None:
        """登录准入:共享限流(可关闭)+ 宿主自有限流 hook,两者都在认证之前。"""

        try:
            if enforce_shared_rate_limits:
                enforce_login(request, username)
            hooks.before_password_login(username, request)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def admit_second_factor_attempt(account_id: str, operation: str, request: Request) -> None:
        """二次验证准入:共享限流(可关闭)+ 宿主自有限流 hook。"""

        try:
            if enforce_shared_rate_limits:
                enforce_second_factor(request, account_id)
            hooks.before_second_factor(account_id, operation, request)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    login_guard = LoginAdmissionGuard(admit_login_attempt)

    _register_login_routes(
        router,
        ports=ports,
        hooks=hooks,
        login_guard=login_guard,
        admit_second_factor_attempt=admit_second_factor_attempt,
    )
    _register_account_self_service_routes(
        router,
        ports=ports,
        hooks=hooks,
        current_user=current_user,
        recovery_user=recovery_user,
        permission_for=permission_for,
        port_call=port_call,
        admit_second_factor_attempt=admit_second_factor_attempt,
    )
    _register_footer_and_notification_routes(
        router,
        ports=ports,
        current_user=current_user,
        permission_for=permission_for,
        port_call=port_call,
    )
    _register_integration_and_ops_routes(
        router,
        ports=ports,
        hooks=hooks,
        current_user=current_user,
        permission_for=permission_for,
        port_call=port_call,
        audited_connection_test=audited_connection_test,
        include_authz_integration=include_authz_integration,
    )
    _apply_route_groups(router, groups, include_authz_integration=include_authz_integration)
    return router


def _register_login_routes(
    router: APIRouter,
    *,
    ports: PlatformPorts,
    hooks: PlatformSecurityHooks,
    login_guard: LoginAdmissionGuard,
    admit_second_factor_attempt,
) -> None:
    @router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
    def login(body: LoginRequest, request: Request) -> LoginResponse:
        return _run_password_login(
            body,
            request,
            account=ports.account,
            login_guard=login_guard,
            admit_second_factor=admit_second_factor_attempt,
            login_event=hooks.login_event,
        )

    @router.post("/auth/login/passkey/begin", response_model=PasskeyLoginBeginResponse, tags=["auth"])
    def passkey_login_begin(body: PasskeyLoginBeginRequest, request: Request) -> PasskeyLoginBeginResponse:
        return _run_passkey_login_begin(
            body,
            request,
            account=ports.account,
            login_guard=login_guard,
            login_event=hooks.login_event,
        )

    @router.post("/auth/login/passkey/complete", response_model=LoginResponse, tags=["auth"])
    def passkey_login_complete(body: PasskeyLoginCompleteRequest, request: Request) -> LoginResponse:
        return _run_passkey_login_complete(
            body,
            request,
            account=ports.account,
            login_guard=login_guard,
            admit_second_factor=admit_second_factor_attempt,
            login_event=hooks.login_event,
        )


def _register_account_self_service_routes(
    router: APIRouter,
    *,
    ports: PlatformPorts,
    hooks: PlatformSecurityHooks,
    current_user,
    recovery_user,
    permission_for,
    port_call,
    admit_second_factor_attempt,
) -> None:
    @router.get("/auth/me", response_model=CurrentUser, tags=["auth"])
    def me(user: CurrentUser = Depends(recovery_user)) -> CurrentUser:
        return hooks.present_current_user(user)

    @router.post("/auth/logout", tags=["auth"])
    def logout(user: CurrentUser = Depends(recovery_user)) -> dict:
        outcome = port_call(lambda: ports.account.revoke_sessions(user.id))
        if isinstance(outcome, dict):
            hooks.after_event(
                user.id,
                "auth.logout",
                None,
                {
                    "localSessionsRevoked": outcome.get("localSessionsRevoked", True),
                    "gatewayLogoutRequired": outcome.get("gatewayLogoutRequired", False),
                },
            )
            return outcome
        hooks.after_event(user.id, "auth.logout", None, {"sessionsRevoked": True})
        return {"ok": True}

    @router.post("/users/me/password", tags=["auth"])
    def change_password(
        body: ChangePasswordRequest, request: Request, user: CurrentUser = Depends(recovery_user)
    ) -> dict[str, bool]:
        hooks.ensure_local_auth_management_allowed(user, "change_password")
        # 「新旧口令相同」是入参校验、不是凭据失败,必须在额度之外先拒掉。
        if body.current_password == body.new_password:
            raise HTTPException(422, "新密码不能与当前密码相同")
        # 改密要校验 currentPassword,是一次真实的口令哈希验证,因此和 TOTP confirm/disable
        # 共用同一份「已登录主体再验证」额度(同一 subject、同一计数键),而不是新开一个桶:
        # 新开桶等于把持有会话的攻击者可强制执行的 argon2/bcrypt 次数翻倍,
        # 那既是猜口令的 oracle,也是 CPU 消耗向量。
        with second_factor_failure_admission(user.id):
            admit_second_factor_attempt(user.id, "change_password", request)
            if not port_call(lambda: ports.account.change_password(user.id, body.current_password, body.new_password)):
                record_second_factor_failure(user.id)
                raise HTTPException(401, "当前密码错误")
            clear_second_factor_failures(user.id)
        hooks.after_event(user.id, "auth.password.change", None, {"sessionsRevoked": True})
        return {"ok": True}

    @router.get("/users/me/totp/status", response_model=TotpStatusResponse, tags=["auth"])
    def totp_status(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTH_TOTP_ADVANCE))
    ) -> TotpStatusResponse:
        hooks.ensure_local_auth_management_allowed(user, "totp_status")
        return TotpStatusResponse(enabled=port_call(lambda: ports.account.totp_status(user.id)))

    @router.post("/users/me/totp/begin", response_model=TotpBeginResponse, tags=["auth"])
    def totp_begin(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTH_TOTP_CREATE))
    ) -> TotpBeginResponse:
        hooks.ensure_local_auth_management_allowed(user, "totp_begin")
        secret, uri = port_call(lambda: ports.account.totp_begin(user.id))
        return TotpBeginResponse(secret=secret, otpauth_uri=uri)

    @router.post("/users/me/totp/confirm", response_model=TotpStatusResponse, tags=["auth"])
    def totp_confirm(
        body: TotpConfirmRequest,
        request: Request,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(AUTH_TOTP_CREATE)),
    ) -> TotpStatusResponse:
        hooks.ensure_local_auth_management_allowed(user, "totp_confirm")
        with second_factor_failure_admission(user.id):
            admit_second_factor_attempt(user.id, "totp_confirm", request)
            if not port_call(lambda: ports.account.totp_confirm(user.id, body.code)):
                record_second_factor_failure(user.id)
                raise HTTPException(401, "验证码错误")
            clear_second_factor_failures(user.id)
        hooks.after_event(user.id, "auth.totp.enable", {"enabled": False}, {"enabled": True, "sessionsRevoked": True})
        return TotpStatusResponse(enabled=True)

    @router.post("/users/me/totp/disable", response_model=TotpStatusResponse, tags=["auth"])
    def totp_disable(
        body: TotpDisableRequest,
        request: Request,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(AUTH_TOTP_ADVANCE)),
    ) -> TotpStatusResponse:
        hooks.ensure_local_auth_management_allowed(user, "totp_disable")
        with second_factor_failure_admission(user.id):
            admit_second_factor_attempt(user.id, "totp_disable", request)
            if not port_call(lambda: ports.account.totp_disable(user.id, body.password, body.code)):
                record_second_factor_failure(user.id)
                raise HTTPException(401, "密码或验证码错误")
            clear_second_factor_failures(user.id)
        hooks.after_event(user.id, "auth.totp.disable", {"enabled": True}, {"enabled": False, "sessionsRevoked": True})
        return TotpStatusResponse(enabled=False)

    @router.get("/users/me/passkeys", response_model=list[PasskeySummary], tags=["auth"])
    def list_passkeys(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTH_PASSKEY_VIEW))
    ) -> list[PasskeySummary]:
        hooks.ensure_local_auth_management_allowed(user, "passkey_list")
        return port_call(lambda: ports.account.list_passkeys(user.id))

    @router.post("/users/me/passkeys/register/begin", response_model=PasskeyRegisterBeginResponse, tags=["auth"])
    def passkey_register_begin(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTH_PASSKEY_CREATE))
    ) -> PasskeyRegisterBeginResponse:
        hooks.ensure_local_auth_management_allowed(user, "passkey_register_begin")
        challenge = port_call(lambda: ports.account.begin_passkey_registration(user.id))
        return PasskeyRegisterBeginResponse(options=challenge.options, state_token=challenge.state_token)

    @router.post(
        "/users/me/passkeys/register/complete",
        response_model=PasskeyRegisterCompleteResponse,
        status_code=201,
        tags=["auth"],
    )
    def passkey_register_complete(
        body: PasskeyRegisterCompleteRequest,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(AUTH_PASSKEY_CREATE)),
    ) -> PasskeyRegisterCompleteResponse:
        hooks.ensure_local_auth_management_allowed(user, "passkey_register_complete")
        item = port_call(
            lambda: ports.account.complete_passkey_registration(
                user.id, body.state_token, body.credential, body.name.strip()
            )
        )
        hooks.after_event(user.id, "auth.passkey.create", None, {"passkeyId": item.id, "sessionsRevoked": True})
        return PasskeyRegisterCompleteResponse(id=item.id, name=item.name)

    @router.delete("/users/me/passkeys/{passkey_id}", status_code=204, tags=["auth"])
    def delete_passkey(
        passkey_id: str,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(AUTH_PASSKEY_CREATE)),
    ) -> Response:
        hooks.ensure_local_auth_management_allowed(user, "passkey_delete")
        if not port_call(lambda: ports.account.delete_passkey(user.id, passkey_id)):
            raise HTTPException(404, "通行密钥不存在")
        hooks.after_event(user.id, "auth.passkey.delete", {"passkeyId": passkey_id}, {"sessionsRevoked": True})
        return Response(status_code=204)


def _register_footer_and_notification_routes(
    router: APIRouter,
    *,
    ports: PlatformPorts,
    current_user,
    permission_for,
    port_call,
) -> None:
    @router.get("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def get_footer() -> FooterSettings:
        return port_call(ports.footer.get_footer)

    @router.put("/app-settings/footer", response_model=FooterSettings, tags=["app-settings"])
    def put_footer(
        body: FooterSettingsUpdate,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(SETTINGS_UPDATE)),
    ) -> FooterSettings:
        footer = FooterSettings(
            footer_html_zh=sanitize_footer_html(body.footer_html_zh),
            footer_html_en=sanitize_footer_html(body.footer_html_en),
        )
        return port_call(lambda: ports.footer.save_footer(footer, actor_id=user.id))

    @router.get("/notifications", response_model=NotificationPage, tags=["notifications"])
    def notifications(
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> NotificationPage:
        return port_call(lambda: ports.notifications.list_notifications(user.id, cursor=cursor, limit=limit))

    @router.post("/notifications/{notification_id}/read", tags=["notifications"])
    def mark_read(
        notification_id: str,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> dict[str, bool]:
        if not port_call(lambda: ports.notifications.mark_read(user.id, notification_id, read_at=datetime.now(UTC))):
            raise HTTPException(404, "通知不存在")
        return {"ok": True}

    @router.post("/notifications/read-all", tags=["notifications"])
    def mark_all_read(
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(NOTIFICATION_CENTER_VIEW)),
    ) -> dict[str, int]:
        return {"updated": port_call(lambda: ports.notifications.mark_all_read(user.id, read_at=datetime.now(UTC)))}


def _register_integration_and_ops_routes(
    router: APIRouter,
    *,
    ports: PlatformPorts,
    hooks: PlatformSecurityHooks,
    current_user,
    permission_for,
    port_call,
    audited_connection_test,
    include_authz_integration: bool,
) -> None:
    @router.get(
        "/identity-integration/settings",
        response_model=OidcSettingsSummary | OidcSettings,
        tags=["identity-integration"],
    )
    def oidc_settings(
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_VIEW)),
    ) -> OidcSettingsSummary | OidcSettings:
        value = port_call(ports.integrations.get_oidc_settings)
        return present_oidc_settings(
            value,
            can_manage=has_permission(user, _permission, IDENTITY_MANAGE),
        )

    @router.put("/identity-integration/settings", response_model=OidcSettings, tags=["identity-integration"])
    def save_oidc_settings(
        body: OidcSettingsUpdate,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_MANAGE)),
    ) -> OidcSettings:
        result = port_call(lambda: ports.integrations.save_oidc_settings(body, actor_id=user.id))
        hooks.after_event(user.id, "identity.settings.update", None, {"enabled": result.enabled})
        return result

    @router.post(
        "/identity-integration/discover", response_model=IdentityDiscoveryResponse, tags=["identity-integration"]
    )
    def discover_oidc(
        body: IdentityDiscoveryRequest,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_MANAGE)),
    ) -> IdentityDiscoveryResponse:
        return port_call(lambda: ports.integrations.discover_oidc(body.issuer))

    @router.get(
        "/identity-integration/directory",
        response_model=DirectorySettingsSummary | DirectorySettings,
        tags=["identity-integration"],
    )
    def directory_settings(
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_VIEW)),
    ) -> DirectorySettingsSummary | DirectorySettings:
        value = port_call(ports.directory.get_directory_settings)
        return present_directory_settings(
            value,
            can_manage=has_permission(user, _permission, IDENTITY_MANAGE),
        )

    @router.put("/identity-integration/directory", response_model=DirectorySettings, tags=["identity-integration"])
    def save_directory_settings(
        body: DirectorySettingsUpdate,
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_MANAGE)),
    ) -> DirectorySettings:
        result = port_call(lambda: ports.directory.save_directory_settings(body, actor_id=user.id))
        hooks.after_event(user.id, "identity.directory.update", None, {"enabled": result.enabled})
        return result

    @router.post(
        "/identity-integration/directory/test", response_model=ConnectionTestResult, tags=["identity-integration"]
    )
    def test_directory(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(IDENTITY_MANAGE))
    ) -> ConnectionTestResult:
        return audited_connection_test(
            ports.directory.test_directory,
            actor_id=user.id,
            action="identity.directory.connection_test",
        )

    @router.post(
        "/identity-integration/directory/sync", response_model=DirectorySyncResult, tags=["identity-integration"]
    )
    def sync_directory(
        user: CurrentUser = Depends(current_user),
        _permission: Any = Depends(permission_for(IDENTITY_MANAGE)),
    ) -> DirectorySyncResult:
        result = port_call(lambda: ports.directory.sync_directory(actor_id=user.id))
        hooks.after_event(user.id, "identity.directory.sync", None, {"status": result.status})
        return result

    @router.post(
        "/identity-integration/connection-test", response_model=ConnectionTestResult, tags=["identity-integration"]
    )
    def test_oidc(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(IDENTITY_MANAGE))
    ) -> ConnectionTestResult:
        return audited_connection_test(
            ports.integrations.test_oidc,
            actor_id=user.id,
            action="identity.connection_test",
        )

    if include_authz_integration:

        @router.get(
            "/authz-integration/status",
            response_model=AuthorizationSettingsSummary | EasyAuthStatus,
            tags=["authz-integration"],
        )
        def easy_auth_status(
            user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTHZ_VIEW))
        ) -> AuthorizationSettingsSummary | EasyAuthStatus:
            value = port_call(ports.integrations.get_easyauth_status)
            return present_authorization_settings(
                value,
                can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
            )

        @router.put("/authz-integration/settings", response_model=EasyAuthStatus, tags=["authz-integration"])
        def save_easy_auth(
            body: EasyAuthSettingsUpdate,
            user: CurrentUser = Depends(current_user),
            _permission: Any = Depends(permission_for(AUTHZ_MANAGE)),
        ) -> EasyAuthStatus:
            return port_call(lambda: ports.integrations.save_easyauth_settings(body, actor_id=user.id))

        @router.post(
            "/authz-integration/connection-test", response_model=ConnectionTestResult, tags=["authz-integration"]
        )
        def test_easy_auth(
            user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(AUTHZ_MANAGE))
        ) -> ConnectionTestResult:
            return audited_connection_test(
                ports.integrations.test_easyauth,
                actor_id=user.id,
                action="authz.connection_test",
            )

    @router.get("/ops/upstream-health", response_model=list[UpstreamHealthItem], tags=["ops"])
    def upstream_health(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(UPSTREAM_VIEW))
    ) -> list[UpstreamHealthItem]:
        return port_call(ports.upstream_health.latest)

    @router.post("/ops/upstream-health/checks", response_model=list[UpstreamHealthItem], tags=["ops"])
    def check_upstreams(
        user: CurrentUser = Depends(current_user), _permission: Any = Depends(permission_for(UPSTREAM_MANAGE))
    ) -> list[UpstreamHealthItem]:
        result = port_call(lambda: ports.upstream_health.run_checks(actor_id=user.id))
        hooks.after_event(user.id, "ops.upstream_health.check", None, {"count": len(result)})
        return result


def _apply_route_groups(router: APIRouter, groups: PlatformRouteGroups, *, include_authz_integration: bool) -> None:
    """按路径筛选 route groups，避免宿主复制共享 route 实现。"""

    def enabled(path: str) -> bool:
        if path.startswith("/auth/login/passkey") or path.startswith("/users/me/passkeys"):
            return groups.passkeys
        if path == "/auth/me":
            return groups.me
        if path == "/auth/logout":
            return groups.auth if groups.logout is None else groups.logout
        if path == "/users/me/password":
            return groups.password
        if path.startswith("/users/me/totp"):
            return groups.totp
        if path.startswith("/auth/"):
            return groups.auth
        if path.startswith("/app-settings/footer"):
            return groups.footer
        if path.startswith("/notifications"):
            return groups.notifications
        if path.startswith("/identity-integration"):
            return groups.identity
        if path.startswith("/authz-integration"):
            return groups.easyauth and include_authz_integration
        if path.startswith("/ops/upstream-health"):
            return groups.upstream
        return True

    router.routes[:] = [route for route in router.routes if enabled(route.path)]
