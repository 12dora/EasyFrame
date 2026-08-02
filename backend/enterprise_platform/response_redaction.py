"""企业集成响应的权限感知脱敏。"""

from __future__ import annotations

from typing import Any

from enterprise_platform.schemas import (
    AuthorizationCatalogItem,
    AuthorizationCatalogSummary,
    AuthorizationConfiguredSummary,
    AuthorizationCredentialSummary,
    AuthorizationSettings,
    AuthorizationSettingsSummary,
    AuthorizationSnapshot,
    AuthorizationSnapshotSummary,
    AuthorizationStatus,
    AuthorizationStatusSummary,
    CurrentUser,
    EasyAuthStatus,
    MyGrantResponse,
    MyGrantSummary,
    OidcSettings,
    OidcSettingsSummary,
)


def has_permission(user: CurrentUser, resolved_permission: Any, code: str) -> bool:
    """兼容宿主 CurrentUser 权限集与 EasyTrade AuthzUser 依赖返回值。"""

    if code in user.permissions:
        return True
    can = getattr(resolved_permission, "can", None)
    return bool(callable(can) and can(code))


def present_oidc_settings(
    value: OidcSettings,
    *,
    can_manage: bool,
) -> OidcSettings | OidcSettingsSummary:
    if can_manage:
        return value
    configured = bool(value.issuer and value.client_id)
    return OidcSettingsSummary(
        enabled=value.enabled,
        configured=configured,
        has_client_secret=value.has_client_secret,
        has_authentik_api_token=value.has_authentik_api_token,
        user_sync_enabled=value.user_sync_enabled,
        user_sync_supported=value.user_sync_supported,
    )


def present_authorization_status(
    value: AuthorizationStatus,
    *,
    can_manage: bool,
) -> AuthorizationStatus | AuthorizationStatusSummary:
    if can_manage:
        return value
    return AuthorizationStatusSummary(
        easyauth=AuthorizationCredentialSummary(
            configured=value.easyauth.configured,
            has_credential=value.easyauth.has_credential,
        ),
        principal=AuthorizationConfiguredSummary(configured=value.principal.mode != "disabled"),
        catalog=AuthorizationConfiguredSummary(configured=value.catalog.total_count > 0),
        snapshots=AuthorizationConfiguredSummary(configured=value.snapshots.total > 0),
    )


def present_authorization_settings(
    value: AuthorizationSettings | EasyAuthStatus,
    *,
    can_manage: bool,
) -> AuthorizationSettings | EasyAuthStatus | AuthorizationSettingsSummary:
    if can_manage:
        return value
    return AuthorizationSettingsSummary(
        configured=value.configured,
        has_credential=value.has_credential,
        permission_request_url=value.permission_request_url,
    )


def present_authorization_catalog(
    values: list[AuthorizationCatalogItem],
    *,
    can_manage: bool,
) -> list[AuthorizationCatalogItem] | list[AuthorizationCatalogSummary]:
    if can_manage:
        return values
    return [
        AuthorizationCatalogSummary(
            name_zh=value.name_zh,
            name_en=value.name_en,
            active=value.active,
        )
        for value in values
    ]


def present_authorization_snapshots(
    values: list[AuthorizationSnapshot],
    *,
    can_manage: bool,
) -> list[AuthorizationSnapshot] | list[AuthorizationSnapshotSummary]:
    if can_manage:
        return values
    return [
        AuthorizationSnapshotSummary(
            user_id=value.user_id,
            display_name=value.display_name,
            grant_count=value.grant_count,
            role_groups=value.role_groups,
            fetched_at=value.fetched_at,
            expires_at=value.expires_at,
            expired=value.expired,
        )
        for value in values
    ]


def present_my_grants(
    values: list[MyGrantResponse],
    *,
    can_manage: bool,
) -> list[MyGrantResponse] | list[MyGrantSummary]:
    if can_manage:
        return values
    return [MyGrantSummary() for _value in values]
