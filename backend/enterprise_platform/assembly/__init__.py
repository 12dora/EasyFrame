"""共享 FastAPI router factory。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

from enterprise_platform.assembly.account_routes import register_account_routes
from enterprise_platform.assembly.contracts import (
    AUTH_PASSKEY_CREATE,
    AUTH_PASSKEY_VIEW,
    AUTH_TOTP_ADVANCE,
    AUTH_TOTP_CREATE,
    AUTHZ_MANAGE,
    AUTHZ_VIEW,
    IDENTITY_MANAGE,
    IDENTITY_VIEW,
    NOTIFICATION_CENTER_VIEW,
    SETTINGS_UPDATE,
    UPSTREAM_MANAGE,
    UPSTREAM_VIEW,
    PlatformPorts,
    PlatformRouteGroups,
    PlatformSecurityHooks,
)
from enterprise_platform.assembly.dependencies import build_assembly_dependencies
from enterprise_platform.assembly.footer_notification_routes import register_footer_notification_routes
from enterprise_platform.assembly.integration_routes import register_integration_routes
from enterprise_platform.assembly.login_routes import register_login_routes
from enterprise_platform.assembly.ops_routes import register_ops_routes
from enterprise_platform.assembly.route_groups import apply_route_groups
from enterprise_platform.auth import authenticate_login as authenticate_login
from enterprise_platform.auth import begin_passkey_login as begin_passkey_login
from enterprise_platform.auth import complete_passkey_login as complete_passkey_login
from enterprise_platform.auth import is_credential_failure as is_credential_failure
from enterprise_platform.schemas import CurrentUser


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
    ctx = build_assembly_dependencies(
        ports,
        hooks=security_hooks or PlatformSecurityHooks(),
        current_user_dependency=current_user_dependency,
        permission_dependency_factory=permission_dependency_factory,
        enforce_shared_rate_limits=enforce_shared_rate_limits,
        include_authz_integration=include_authz_integration,
    )
    register_login_routes(router, ctx)
    register_account_routes(router, ctx)
    register_footer_notification_routes(router, ctx)
    register_integration_routes(router, ctx)
    register_ops_routes(router, ctx)
    apply_route_groups(
        router, route_groups or PlatformRouteGroups(), include_authz_integration=include_authz_integration
    )
    return router


__all__ = [
    "AUTHZ_MANAGE",
    "AUTHZ_VIEW",
    "AUTH_PASSKEY_CREATE",
    "AUTH_PASSKEY_VIEW",
    "AUTH_TOTP_ADVANCE",
    "AUTH_TOTP_CREATE",
    "IDENTITY_MANAGE",
    "IDENTITY_VIEW",
    "NOTIFICATION_CENTER_VIEW",
    "SETTINGS_UPDATE",
    "UPSTREAM_MANAGE",
    "UPSTREAM_VIEW",
    "PlatformPorts",
    "PlatformRouteGroups",
    "PlatformSecurityHooks",
    "authenticate_login",
    "begin_passkey_login",
    "complete_passkey_login",
    "create_platform_router",
    "is_credential_failure",
]
