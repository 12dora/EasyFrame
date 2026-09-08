"""身份集成与授权集成路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from enterprise_platform.assembly.contracts import AUTHZ_MANAGE, AUTHZ_VIEW, IDENTITY_MANAGE, IDENTITY_VIEW
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.assembly.easyauth_event_routes import register_easyauth_event_routes
from enterprise_platform.response_redaction import (
    has_permission,
    present_authorization_settings,
    present_directory_settings,
    present_oidc_settings,
)
from enterprise_platform.schemas import (
    AuthorizationSettingsSummary,
    ConnectionTestResult,
    CurrentUser,
    DirectorySettings,
    DirectorySettingsSummary,
    DirectorySettingsUpdate,
    DirectorySyncResult,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    IdentityDiscoveryRequest,
    IdentityDiscoveryResponse,
    OidcSettings,
    OidcSettingsSummary,
    OidcSettingsUpdate,
)


def register_integration_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_oidc_settings(router, ctx)
    _register_save_oidc(router, ctx)
    _register_discover_oidc(router, ctx)
    _register_directory_settings(router, ctx)
    _register_save_directory(router, ctx)
    _register_test_directory(router, ctx)
    _register_sync_directory(router, ctx)
    _register_test_oidc(router, ctx)
    register_easyauth_event_routes(router, ctx)
    if ctx.include_authz_integration:
        _register_easy_auth_status(router, ctx)
        _register_save_easy_auth(router, ctx)
        _register_test_easy_auth(router, ctx)


def _register_oidc_settings(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get(
        "/identity-integration/settings",
        response_model=OidcSettingsSummary | OidcSettings,
        tags=["identity-integration"],
    )
    def oidc_settings(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_VIEW)),
    ) -> OidcSettingsSummary | OidcSettings:
        value = ctx.port_call(ctx.ports.integrations.get_oidc_settings)
        return present_oidc_settings(value, can_manage=has_permission(user, _permission, IDENTITY_MANAGE))


def _register_save_oidc(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/identity-integration/settings", response_model=OidcSettings, tags=["identity-integration"])
    def save_oidc_settings(
        body: OidcSettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> OidcSettings:
        result = ctx.port_call(lambda: ctx.ports.integrations.save_oidc_settings(body, actor_id=user.id))
        ctx.hooks.after_event(user.id, "identity.settings.update", None, {"enabled": result.enabled})
        return result


def _register_discover_oidc(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post(
        "/identity-integration/discover", response_model=IdentityDiscoveryResponse, tags=["identity-integration"]
    )
    def discover_oidc(
        body: IdentityDiscoveryRequest,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> IdentityDiscoveryResponse:
        return ctx.port_call(lambda: ctx.ports.integrations.discover_oidc(body.issuer))


def _register_directory_settings(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get(
        "/identity-integration/directory",
        response_model=DirectorySettingsSummary | DirectorySettings,
        tags=["identity-integration"],
    )
    def directory_settings(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_VIEW)),
    ) -> DirectorySettingsSummary | DirectorySettings:
        value = ctx.port_call(ctx.ports.directory.get_directory_settings)
        return present_directory_settings(value, can_manage=has_permission(user, _permission, IDENTITY_MANAGE))


def _register_save_directory(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/identity-integration/directory", response_model=DirectorySettings, tags=["identity-integration"])
    def save_directory_settings(
        body: DirectorySettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> DirectorySettings:
        result = ctx.port_call(lambda: ctx.ports.directory.save_directory_settings(body, actor_id=user.id))
        ctx.hooks.after_event(user.id, "identity.directory.update", None, {"enabled": result.enabled})
        return result


def _register_test_directory(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post(
        "/identity-integration/directory/test", response_model=ConnectionTestResult, tags=["identity-integration"]
    )
    def test_directory(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> ConnectionTestResult:
        return ctx.audited_connection_test(
            ctx.ports.directory.test_directory,
            actor_id=user.id,
            action="identity.directory.connection_test",
        )


def _register_sync_directory(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post(
        "/identity-integration/directory/sync", response_model=DirectorySyncResult, tags=["identity-integration"]
    )
    def sync_directory(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> DirectorySyncResult:
        result = ctx.port_call(lambda: ctx.ports.directory.sync_directory(actor_id=user.id))
        ctx.hooks.after_event(user.id, "identity.directory.sync", None, {"status": result.status})
        return result


def _register_test_oidc(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post(
        "/identity-integration/connection-test", response_model=ConnectionTestResult, tags=["identity-integration"]
    )
    def test_oidc(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(IDENTITY_MANAGE)),
    ) -> ConnectionTestResult:
        return ctx.audited_connection_test(
            ctx.ports.integrations.test_oidc,
            actor_id=user.id,
            action="identity.connection_test",
        )


def _register_easy_auth_status(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get(
        "/authz-integration/status",
        response_model=AuthorizationSettingsSummary | EasyAuthStatus,
        tags=["authz-integration"],
    )
    def easy_auth_status(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTHZ_VIEW)),
    ) -> AuthorizationSettingsSummary | EasyAuthStatus:
        value = ctx.port_call(ctx.ports.integrations.get_easyauth_status)
        return present_authorization_settings(value, can_manage=has_permission(user, _permission, AUTHZ_MANAGE))


def _register_save_easy_auth(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.put("/authz-integration/settings", response_model=EasyAuthStatus, tags=["authz-integration"])
    def save_easy_auth(
        body: EasyAuthSettingsUpdate,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTHZ_MANAGE)),
    ) -> EasyAuthStatus:
        return ctx.port_call(lambda: ctx.ports.integrations.save_easyauth_settings(body, actor_id=user.id))


def _register_test_easy_auth(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/authz-integration/connection-test", response_model=ConnectionTestResult, tags=["authz-integration"])
    def test_easy_auth(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(AUTHZ_MANAGE)),
    ) -> ConnectionTestResult:
        return ctx.audited_connection_test(
            ctx.ports.integrations.test_easyauth,
            actor_id=user.id,
            action="authz.connection_test",
        )
