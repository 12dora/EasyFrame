"""企业框架站共享 HTTP 合同。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic.alias_generators import to_camel


class PlatformModel(BaseModel):
    model_config = {
        "alias_generator": to_camel,
        "validate_by_alias": True,
        "validate_by_name": True,
        "populate_by_name": True,
        "from_attributes": True,
    }


class RedactedPlatformModel(PlatformModel):
    """只读脱敏响应；禁止意外夹带完整配置字段。"""

    model_config = {
        **PlatformModel.model_config,
        "extra": "forbid",
    }


class StrictPlatformModel(PlatformModel):
    model_config = {
        **PlatformModel.model_config,
        "populate_by_name": False,
        "validate_by_name": False,
        "validate_by_alias": True,
        "extra": "forbid",
    }


class LoginRequest(StrictPlatformModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    totp_code: str | None = None


class LoginResponse(PlatformModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class ChangePasswordRequest(StrictPlatformModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class PasskeyLoginBeginRequest(StrictPlatformModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class PasskeyLoginBeginResponse(PlatformModel):
    options: dict[str, Any]
    state_token: str


class PasskeyLoginCompleteRequest(StrictPlatformModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    state_token: str = Field(min_length=1)
    credential: dict[str, Any]


class TotpStatusResponse(PlatformModel):
    enabled: bool


class TotpBeginResponse(PlatformModel):
    secret: str
    otpauth_uri: str


class TotpConfirmRequest(StrictPlatformModel):
    code: str = Field(min_length=6, max_length=6)


class TotpDisableRequest(StrictPlatformModel):
    password: str = Field(min_length=1)
    code: str = Field(min_length=6, max_length=6)


class PasskeySummary(PlatformModel):
    id: str
    name: str
    created_at: datetime | None
    last_used_at: datetime | None


class PasskeyRegisterBeginResponse(PlatformModel):
    options: dict[str, Any]
    state_token: str


class PasskeyRegisterCompleteRequest(StrictPlatformModel):
    state_token: str = Field(min_length=1)
    credential: dict[str, Any]
    name: str = Field(min_length=1, max_length=100)


class PasskeyRegisterCompleteResponse(PlatformModel):
    id: str
    name: str


class FooterSettings(PlatformModel):
    footer_html_zh: str
    footer_html_en: str


class FooterSettingsUpdate(StrictPlatformModel):
    footer_html_zh: str = Field(max_length=20_000)
    footer_html_en: str = Field(max_length=20_000)


class SecurityCapabilities(PlatformModel):
    """宿主按运行模式公开的本地认证能力；前端仍须与权限取交集。"""

    password_change: bool = False
    totp_status: bool = False
    totp_enroll: bool = False
    totp_disable: bool = False
    passkey_list: bool = False
    passkey_register: bool = False
    passkey_delete: bool = False


class CurrentUser(PlatformModel):
    id: str
    name: str
    email: str | None = None
    avatar_url: str | None = None
    ui_locale: str = "zh-CN"
    must_change_password: bool = False
    has_local_password: bool = False
    permissions: list[str] = Field(default_factory=list)
    role_groups: list[str] = Field(default_factory=list)
    security_capabilities: SecurityCapabilities = Field(default_factory=SecurityCapabilities)


NotificationLevel = Literal["info", "success", "warning", "error"]


class NotificationItem(PlatformModel):
    id: str
    title: str
    title_code: str | None = None
    body: str
    body_code: str | None = None
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    level: NotificationLevel = "info"
    created_at: datetime
    read_at: datetime | None = None
    href: str | None = None


class NotificationPage(PlatformModel):
    items: list[NotificationItem]
    unread_count: int
    next_cursor: str | None = None


HealthStatus = Literal["healthy", "warning", "unhealthy", "unknown"]


class UpstreamHealthItem(PlatformModel):
    dependency: str
    display_name: str
    status: HealthStatus
    checked_at: datetime | None
    summary: str
    error_summary: str = ""
    summary_code: str = "upstream.unknown"
    summary_params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    supported: bool = True


class OidcSettings(PlatformModel):
    enabled: bool = False
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    userinfo_endpoint: str = ""
    client_id: str = ""
    has_client_secret: bool = False
    scopes: str = "openid profile email"
    redirect_base_url: str = ""
    redirect_uri: str = ""
    frontend_base_url: str = ""
    server_base_url: str = ""
    authentik_api_base_url: str = ""
    has_authentik_api_token: bool = False
    user_sync_enabled: bool = False
    user_sync_interval_minutes: int = 30
    user_sync_supported: bool = False


class OidcSettingsSummary(RedactedPlatformModel):
    """无管理权限时仅返回业务状态，不返回 OIDC 协议或凭据标识。"""

    enabled: bool = False
    configured: bool = False
    has_client_secret: bool = False
    has_authentik_api_token: bool = False
    user_sync_enabled: bool = False
    user_sync_supported: bool = False


class OidcSettingsUpdate(StrictPlatformModel):
    enabled: bool
    issuer: str = Field(max_length=2000)
    authorization_endpoint: str = Field(max_length=2000)
    token_endpoint: str = Field(max_length=2000)
    jwks_uri: str = Field(max_length=2000)
    userinfo_endpoint: str = Field(default="", max_length=2000)
    client_id: str = Field(max_length=200)
    client_secret: str | None = Field(default=None, max_length=2000)
    scopes: str = Field(min_length=1, max_length=400)
    redirect_base_url: str = Field(max_length=2000)
    frontend_base_url: str = Field(max_length=2000)
    server_base_url: str = Field(default="", max_length=2000)
    authentik_api_base_url: str = Field(default="", max_length=2000)
    authentik_api_token: str | None = Field(default=None, max_length=2000)
    user_sync_enabled: bool = False
    user_sync_interval_minutes: int = Field(default=30, ge=1, le=1440)

    @field_validator(
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "userinfo_endpoint",
        "redirect_base_url",
        "frontend_base_url",
        "authentik_api_base_url",
    )
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        from enterprise_platform.urls import validate_endpoint_url

        return validate_endpoint_url(value)

    @field_validator("server_base_url")
    @classmethod
    def validate_server_authority(cls, value: str) -> str:
        from enterprise_platform.urls import validate_server_base_url

        return validate_server_base_url(value)


class EasyAuthStatus(PlatformModel):
    configured: bool = False
    base_url: str = ""
    app_key: str = ""
    auth_mode: str = ""
    has_credential: bool = False
    permission_request_url: str = ""


class EasyAuthSettingsUpdate(StrictPlatformModel):
    base_url: str = Field(max_length=2000)
    app_key: str = Field(max_length=200)
    credential: str | None = Field(default=None, max_length=4000)
    permission_request_url: str = Field(default="", max_length=2000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        from enterprise_platform.urls import validate_endpoint_url

        return validate_endpoint_url(value)

    @field_validator("permission_request_url")
    @classmethod
    def validate_request_url(cls, value: str) -> str:
        from enterprise_platform.urls import validate_permission_request_url

        return validate_permission_request_url(value)


class PermissionRequestUrlUpdate(PlatformModel):
    """只更新权限申请入口，不要求 EasyAuth 连接已经配置。"""

    permission_request_url: str = Field(default="", max_length=2000)

    @field_validator("permission_request_url")
    @classmethod
    def validate_request_url(cls, value: str) -> str:
        from enterprise_platform.urls import validate_permission_request_url

        return validate_permission_request_url(value)


class ConnectionTestResult(PlatformModel):
    ok: bool
    latency_ms: int = 0
    error_kind: str | None = None
    error_detail: str | None = None


class IdentityDiscoveryRequest(StrictPlatformModel):
    issuer: str | None = Field(default=None, max_length=2000)

    @field_validator("issuer")
    @classmethod
    def validate_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from enterprise_platform.urls import validate_endpoint_url

        return validate_endpoint_url(value)


class IdentityDiscoveryResponse(PlatformModel):
    ok: bool
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    userinfo_endpoint: str = ""
    error_kind: str | None = None
    error_detail: str | None = None


class UserSyncCapabilityResponse(PlatformModel):
    supported: bool
    status: Literal["completed", "not_supported", "not_configured", "failed"]
    summary: str
    upstream_total: int = 0
    matched: int = 0
    updated: int = 0
    deactivated: int = 0
    reactivated: int = 0


class MyGrantResponse(PlatformModel):
    permission: str
    data_scope: str
    source: str | None = None
    resolved_user_ids: list[str] = Field(default_factory=list)


class DescriptorKeyResponse(PlatformModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    active: bool
    last_used_at: datetime | None = None
    created_at: datetime


class DescriptorKeyCreateRequest(StrictPlatformModel):
    name: str = Field(min_length=1, max_length=120)


class DescriptorKeyCreateResponse(PlatformModel):
    key: DescriptorKeyResponse
    token: str


class DescriptorKeyUpdateRequest(StrictPlatformModel):
    active: bool


class AuthorizationEasyAuthSummary(PlatformModel):
    configured: bool = False
    base_url: str = ""
    app_key: str = ""
    auth_mode: str = ""
    has_credential: bool = False
    timeout_seconds: float = 5


class AuthorizationPrincipalSummary(PlatformModel):
    mode: str = "disabled"
    header_name: str = ""
    issuer: str | None = None
    audience: str | None = None


class AuthorizationCountStats(PlatformModel):
    active_count: int = 0
    total_count: int = 0


class AuthorizationSnapshotStats(PlatformModel):
    total: int = 0
    expired: int = 0
    latest_fetched_at: datetime | None = None


class AuthorizationStatus(PlatformModel):
    easyauth: AuthorizationEasyAuthSummary
    principal: AuthorizationPrincipalSummary
    catalog: AuthorizationCountStats
    snapshots: AuthorizationSnapshotStats


class AuthorizationConfiguredSummary(RedactedPlatformModel):
    configured: bool = False


class AuthorizationCredentialSummary(RedactedPlatformModel):
    configured: bool = False
    has_credential: bool = False


class AuthorizationStatusSummary(RedactedPlatformModel):
    """无管理权限时的 EasyAuth 粗粒度业务状态。"""

    easyauth: AuthorizationCredentialSummary
    principal: AuthorizationConfiguredSummary
    catalog: AuthorizationConfiguredSummary
    snapshots: AuthorizationConfiguredSummary


class AuthorizationConnectionError(PlatformModel):
    kind: str
    message: str


class AuthorizationConnectionResult(PlatformModel):
    ok: bool
    latency_ms: int = 0
    snapshot_version: str | None = None
    grant_count: int | None = None
    error: AuthorizationConnectionError | None = None


class AuthorizationSettings(PlatformModel):
    configured: bool = False
    base_url: str = ""
    app_key: str = ""
    auth_mode: str = ""
    has_credential: bool = False
    permission_request_url: str = ""


class AuthorizationSettingsSummary(RedactedPlatformModel):
    configured: bool = False
    has_credential: bool = False
    permission_request_url: str = ""


class AuthorizationSettingsUpdate(StrictPlatformModel):
    """统一更新合同。部署托管宿主可只接受 permissionRequestUrl。"""

    base_url: str | None = Field(default=None, max_length=2000)
    app_key: str | None = Field(default=None, max_length=200)
    credential: str | None = Field(default=None, max_length=4000)
    permission_request_url: str = Field(default="", max_length=2000)

    @field_validator("base_url")
    @classmethod
    def validate_optional_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from enterprise_platform.urls import validate_endpoint_url

        return validate_endpoint_url(value)

    @field_validator("permission_request_url")
    @classmethod
    def validate_optional_request_url(cls, value: str) -> str:
        from enterprise_platform.urls import validate_permission_request_url

        return validate_permission_request_url(value)


class AuthorizationCatalogItem(PlatformModel):
    code: str
    name_zh: str
    name_en: str
    domain: str
    resource: str
    action: str = ""
    supported_scopes: list[str] = Field(default_factory=list)
    risk_level: str
    active: bool


class AuthorizationCatalogSummary(RedactedPlatformModel):
    name_zh: str
    name_en: str
    active: bool


class AuthorizationSnapshot(PlatformModel):
    user_id: uuid.UUID
    display_name: str | None = None
    external_user_id: str
    app_key: str
    grant_count: int
    grant_version: int
    catalog_version: int
    snapshot_version: str
    role_groups: list[str] = Field(default_factory=list)
    fetched_at: datetime
    expires_at: datetime
    expired: bool
    # 刷新合同:版本门禁忽略较旧上游响应时仍可能返回未过期缓存,须显式声明未更新。
    refreshed: bool = True
    refresh_reason: str | None = None


class AuthorizationSnapshotSummary(RedactedPlatformModel):
    user_id: uuid.UUID
    display_name: str | None = None
    grant_count: int
    role_groups: list[str] = Field(default_factory=list)
    fetched_at: datetime
    expires_at: datetime
    expired: bool


class MyGrantSummary(RedactedPlatformModel):
    active: bool = True
