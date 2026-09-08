"""Assembly 合同:权限常量、ports、安全 hooks 与路由分组开关。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from enterprise_platform.ports import (
    AccountPort,
    AppSettingsPort,
    AuthorizationOperationsPort,
    DirectoryPort,
    IntegrationPort,
    NotificationPort,
    PermissionCheck,
    UpstreamHealthPort,
)
from enterprise_platform.schemas import CurrentUser

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
    app_settings: AppSettingsPort
    notifications: NotificationPort
    integrations: IntegrationPort
    directory: DirectoryPort
    upstream_health: UpstreamHealthPort
    require_permission: PermissionCheck
    authorization: AuthorizationOperationsPort | None = None


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
    # 规范开关为 app_settings;footer 为兼容别名,None 时跟随 app_settings。
    app_settings: bool = True
    footer: bool | None = None
    notifications: bool = True
    identity: bool = True
    easyauth: bool = True
    upstream: bool = True
