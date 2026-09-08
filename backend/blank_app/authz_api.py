"""blank host 的 EasyAuth catalog、manifest 与 snapshot 持久化适配。

物理实现:
- ``authz_snapshot``: catalog 种子、权限客户端、快照持久化
- ``authz_descriptor``: well-known descriptor、manifest、同步密钥
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from blank_app import authz_descriptor, authz_snapshot
from blank_app.adapters import (
    ALL_PERMISSIONS,
    BlankIntegrationAdapter,
    _get_setting,
    account_adapter,
    is_unsafe_bootstrap_secret,
    record_platform_audit,
    require_permission,
)
from blank_app.database import SessionLocal
from blank_app.models import Account, DescriptorKey, PermissionCatalog, PermissionSnapshot, PlatformSetting
from blank_app.permission_registry import FRAMEWORK_PERMISSIONS
from enterprise_platform.auth import AuthError
from enterprise_platform.authorization import create_authorization_operations_router
from enterprise_platform.authz import (
    CatalogPermission,
    DataScope,
    EasyAuthClientError,
    EasyAuthForbiddenError,
    EasyAuthPermissionClient,
    PermissionManifestRegistration,
    PermissionManifestRegistry,
    PrincipalValidationError,
    RiskLevel,
    classify_connection_failure,
    normalize_catalog_risk_level,
    normalize_grants,
    parse_upstream_principal_from_headers,
)
from enterprise_platform.schemas import (
    AuthorizationCatalogItem,
    AuthorizationConnectionResult,
    AuthorizationSettings,
    AuthorizationSettingsUpdate,
    AuthorizationSnapshot,
    AuthorizationStatus,
    ConnectionTestResult,
    CurrentUser,
    DescriptorKeyCreateRequest,
    DescriptorKeyCreateResponse,
    DescriptorKeyResponse,
    DescriptorKeyUpdateRequest,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    MyGrantResponse,
    PlatformModel,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret

SnapshotResponse = authz_snapshot.SnapshotResponse
_catalog_map = authz_snapshot._catalog_map
_close_permission_client = authz_snapshot._close_permission_client
_commit_snapshot_row = authz_snapshot._commit_snapshot_row
_permission_client = authz_snapshot._permission_client
_role_groups = authz_snapshot._role_groups
_utc = authz_snapshot._utc
ensure_account_snapshot = authz_snapshot.ensure_account_snapshot
refresh_account_snapshot = authz_snapshot.refresh_account_snapshot
seed_platform_catalog = authz_snapshot.seed_platform_catalog
snapshot_grants_for_account = authz_snapshot.snapshot_grants_for_account

_create_descriptor_key = authz_descriptor._create_descriptor_key
_current_manifest = authz_descriptor._current_manifest
_delete_descriptor_key = authz_descriptor._delete_descriptor_key
_list_descriptor_keys = authz_descriptor._list_descriptor_keys
_update_descriptor_key = authz_descriptor._update_descriptor_key
_validate_descriptor_token = authz_descriptor._validate_descriptor_token
descriptor_router = authz_descriptor.descriptor_router
easyauth_descriptor = authz_descriptor.easyauth_descriptor

FRAMEWORK_MANIFEST = PermissionManifestRegistration(
    schema_version=1,
    app_key="enterprise-blank",
    permissions=FRAMEWORK_PERMISSIONS,
)
PermissionManifestRegistry(FRAMEWORK_MANIFEST.permissions)


def validate_principal_config() -> None:
    """启动时验证可信 principal verifier，避免错误配置拖到首请求才暴露。"""

    mode = os.getenv("BLANK_PRINCIPAL_MODE", "disabled")
    if mode not in {"disabled", "jwt", "header"}:
        raise RuntimeError("BLANK_PRINCIPAL_MODE must be one of disabled, jwt, header")
    if mode == "disabled":
        return
    required = {
        "BLANK_PRINCIPAL_HEADER": os.getenv("BLANK_PRINCIPAL_HEADER", "X-Enterprise-Principal-JWT"),
        "BLANK_PRINCIPAL_ISSUER": os.getenv("BLANK_PRINCIPAL_ISSUER", ""),
        "BLANK_PRINCIPAL_AUDIENCE": os.getenv("BLANK_PRINCIPAL_AUDIENCE", ""),
        "BLANK_PRINCIPAL_SECRET": os.getenv("BLANK_PRINCIPAL_SECRET", ""),
    }
    missing = [key for key, value in required.items() if not value.strip()]
    if missing:
        raise RuntimeError("trusted principal configuration is incomplete: " + ", ".join(missing))
    if is_unsafe_bootstrap_secret(required["BLANK_PRINCIPAL_SECRET"], min_length=32):
        raise RuntimeError("BLANK_PRINCIPAL_SECRET must be at least 32 bytes and must not use a public example")
    try:
        max_lifetime = int(os.getenv("BLANK_PRINCIPAL_MAX_LIFETIME_SECONDS", "300"))
    except ValueError as exc:
        raise RuntimeError("BLANK_PRINCIPAL_MAX_LIFETIME_SECONDS must be an integer") from exc
    if not 30 <= max_lifetime <= 600:
        raise RuntimeError("BLANK_PRINCIPAL_MAX_LIFETIME_SECONDS must be between 30 and 600")


class CatalogItemResponse(PlatformModel):
    code: str
    name_zh: str
    name_en: str
    domain: str
    resource: str
    supported_scopes: list[str]
    risk_level: RiskLevel
    active: bool
    action: str = ""


class ConnectionErrorResponse(PlatformModel):
    kind: str
    message: str


class AuthzConnectionResponse(PlatformModel):
    ok: bool
    latency_ms: int
    snapshot_version: str | None = None
    grant_count: int | None = None
    error: ConnectionErrorResponse | None = None


class EasyAuthSummaryResponse(PlatformModel):
    configured: bool
    base_url: str
    app_key: str
    auth_mode: str
    has_credential: bool
    timeout_seconds: float


class PrincipalSummaryResponse(PlatformModel):
    mode: str
    header_name: str
    issuer: str | None
    audience: str | None


class CountStatsResponse(PlatformModel):
    active_count: int
    total_count: int


class SnapshotStatsResponse(PlatformModel):
    total: int
    expired: int
    latest_fetched_at: datetime | None


class AuthzStatusResponse(PlatformModel):
    easyauth: EasyAuthSummaryResponse
    principal: PrincipalSummaryResponse
    catalog: CountStatsResponse
    snapshots: SnapshotStatsResponse


def resolve_trusted_principal(headers: dict[str, str]) -> str | None:
    principal = parse_upstream_principal_from_headers(
        headers,
        mode=os.getenv("BLANK_PRINCIPAL_MODE", "disabled"),
        header_name=os.getenv("BLANK_PRINCIPAL_HEADER", "X-Enterprise-Principal-JWT"),
        issuer=os.getenv("BLANK_PRINCIPAL_ISSUER") or None,
        audience=os.getenv("BLANK_PRINCIPAL_AUDIENCE") or None,
        secret=os.getenv("BLANK_PRINCIPAL_SECRET") or None,
        max_lifetime_seconds=int(os.getenv("BLANK_PRINCIPAL_MAX_LIFETIME_SECONDS", "300")),
    )
    if principal is None:
        return None
    with SessionLocal() as db:
        account = (
            db.query(Account)
            .filter(
                Account.external_source == principal.external_source,
                Account.external_user_id == principal.sub,
            )
            .one_or_none()
        )
        if account is not None and not account.active:
            raise PrincipalValidationError("inactive local user")
        if account is None:
            username = principal.name
            if db.query(Account.id).filter(Account.username == username).first() is not None:
                username = f"{principal.name}-{principal.sub[:8]}"
            account = Account(
                username=username,
                email=principal.email,
                password_hash=None,
                external_source=principal.external_source,
                external_user_id=principal.sub,
                active=True,
                is_admin=False,
                must_change_password=False,
            )
            db.add(account)
        account.email = principal.email
        account.avatar_url = principal.avatar_url
        if principal.ui_locale:
            account.ui_locale = principal.ui_locale
        db.commit()
        db.refresh(account)
        account_id = str(account.id)
    ensure_account_snapshot(account_id, force=False)
    return account_id


def _current_user() -> CurrentUser:
    user = account_adapter.current_user()
    if user.must_change_password:
        raise AuthError(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
    return user


def test_easyauth_connection() -> ConnectionTestResult:
    return BlankIntegrationAdapter().test_easyauth()


def _authorization_status() -> AuthzStatusResponse:
    """读取 Blank 授权接入状态；权限校验由共享 router 统一执行。"""

    data = _get_setting("easyauth")
    now = datetime.now(UTC)
    with SessionLocal() as db:
        catalog_total = db.query(PermissionCatalog).count()
        catalog_active = db.query(PermissionCatalog).filter(PermissionCatalog.active.is_(True)).count()
        snapshot_total = db.query(PermissionSnapshot).count()
        rows = db.query(PermissionSnapshot).all()
        latest = max((_utc(row.fetched_at) for row in rows), default=None)
        expired = sum(_utc(row.expires_at) <= now for row in rows)
    return AuthzStatusResponse(
        easyauth=EasyAuthSummaryResponse(
            configured=bool(data.get("base_url") and data.get("app_key") and data.get("credential")),
            base_url=str(data.get("base_url") or ""),
            app_key=str(data.get("app_key") or FRAMEWORK_MANIFEST.app_key),
            auth_mode=str(data.get("auth_mode") or "static_app_token"),
            has_credential=bool(data.get("credential")),
            timeout_seconds=5,
        ),
        principal=PrincipalSummaryResponse(
            mode=os.getenv("BLANK_PRINCIPAL_MODE", "disabled"),
            header_name=os.getenv("BLANK_PRINCIPAL_HEADER", "X-Enterprise-Principal-JWT"),
            issuer=os.getenv("BLANK_PRINCIPAL_ISSUER") or None,
            audience=os.getenv("BLANK_PRINCIPAL_AUDIENCE") or None,
        ),
        catalog=CountStatsResponse(active_count=catalog_active, total_count=catalog_total),
        snapshots=SnapshotStatsResponse(
            total=snapshot_total,
            expired=expired,
            latest_fetched_at=latest,
        ),
    )


def _test_authorization_connection(*, actor_id: str) -> AuthzConnectionResponse:
    """探测 EasyAuth 并以显式操作者记录脱敏审计。"""

    started = datetime.now(UTC)
    client: object | None = None
    try:
        client = _permission_client()
        snapshot = client.fetch_permission_snapshot("enterprise-platform-connectivity-probe")
    except EasyAuthForbiddenError as exc:
        kind = classify_connection_failure(str(exc), forbidden=True)
        result = AuthzConnectionResponse(
            ok=False,
            latency_ms=_latency_ms(started),
            error=ConnectionErrorResponse(kind=kind.value, message=str(exc)),
        )
    except EasyAuthClientError as exc:
        kind = classify_connection_failure(str(exc), network_error=isinstance(exc.__cause__, httpx.RequestError))
        result = AuthzConnectionResponse(
            ok=False,
            latency_ms=_latency_ms(started),
            error=ConnectionErrorResponse(kind=kind.value, message=str(exc)),
        )
    else:
        result = AuthzConnectionResponse(
            ok=True,
            latency_ms=_latency_ms(started),
            snapshot_version=snapshot.snapshot_version,
            grant_count=len(snapshot.grants),
        )
    finally:
        if client is not None:
            _close_permission_client(client)
    record_platform_audit(
        actor_id,
        "authz.connection_test",
        None,
        {
            "ok": result.ok,
            "latencyMs": result.latency_ms,
            "errorKind": result.error.kind if result.error else None,
        },
    )
    return result


def _authorization_settings() -> EasyAuthStatus:
    return EasyAuthStatus.model_validate(_get_setting("easyauth") or {})


def _my_grants(*, actor_id: str) -> list[MyGrantResponse]:
    with SessionLocal() as db:
        account = db.get(Account, actor_id)
        if account is None:
            raise HTTPException(401, "登录态无效")
        if account.is_admin:
            return [
                MyGrantResponse(permission=code, data_scope="ALL", source="local-break-glass")
                for code in sorted(ALL_PERMISSIONS)
            ]
        setting = db.get(PlatformSetting, "easyauth")
        app_key = str((setting.value if setting else {}).get("app_key") or "")
        snapshot = (
            db.query(PermissionSnapshot)
            .filter(
                PermissionSnapshot.account_id == account.id,
                PermissionSnapshot.app_key == app_key,
                PermissionSnapshot.expires_at > datetime.now(UTC),
            )
            .one_or_none()
        )
        if snapshot is None:
            return []
        return [
            MyGrantResponse(
                permission=grant.permission_code,
                data_scope=grant.data_scope.value,
                source=grant.source,
                resolved_user_ids=list(grant.resolved.user_ids) if grant.resolved else [],
            )
            for grant in normalize_grants(snapshot.grants, _catalog_map(db))
        ]


def _latency_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)


class BlankAuthorizationOperations:
    """Blank 对共享授权运维 port 的持久化/上游/审计适配。"""

    def status(self) -> AuthorizationStatus:
        return AuthorizationStatus.model_validate(_authorization_status().model_dump(by_alias=True))

    def connection_test(self, *, actor_id: str) -> AuthorizationConnectionResult:
        result = _test_authorization_connection(actor_id=actor_id)
        return AuthorizationConnectionResult.model_validate(result.model_dump(by_alias=True))

    def get_settings(self) -> AuthorizationSettings:
        value = _authorization_settings()
        return AuthorizationSettings.model_validate(value.model_dump(by_alias=True))

    def save_settings(self, payload: AuthorizationSettingsUpdate, *, actor_id: str) -> AuthorizationSettings:
        return BlankIntegrationAdapter().save_authorization_update(payload, actor_id=actor_id)

    def list_catalog(self, *, search: str | None, active: bool | None) -> list[AuthorizationCatalogItem]:
        with SessionLocal() as db:
            query = db.query(PermissionCatalog)
            if active is not None:
                query = query.filter(PermissionCatalog.active.is_(active))
            if search and search.strip():
                term = f"%{search.strip()}%"
                query = query.filter(
                    or_(
                        PermissionCatalog.code.ilike(term),
                        PermissionCatalog.name_zh.ilike(term),
                        PermissionCatalog.name_en.ilike(term),
                    )
                )
            rows = query.order_by(PermissionCatalog.code.asc()).all()
            return [
                AuthorizationCatalogItem.model_validate(
                    {
                        **CatalogItemResponse.model_validate(
                            {
                                "code": row.code,
                                "name_zh": row.name_zh,
                                "name_en": row.name_en,
                                "domain": row.domain,
                                "resource": row.resource,
                                "supported_scopes": row.supported_scopes,
                                "risk_level": normalize_catalog_risk_level(row.risk_level),
                                "active": row.active,
                            }
                        ).model_dump(),
                        "action": row.code.rsplit(".", 1)[-1],
                    }
                )
                for row in rows
            ]

    def list_snapshots(
        self, *, search: str | None, offset: int, limit: int, sort: str
    ) -> tuple[list[AuthorizationSnapshot], int]:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            query = db.query(PermissionSnapshot, Account.username).join(
                Account, PermissionSnapshot.account_id == Account.id
            )
            if search and search.strip():
                term = f"%{search.strip()}%"
                query = query.filter(
                    or_(
                        PermissionSnapshot.external_user_id.ilike(term),
                        PermissionSnapshot.snapshot_version.ilike(term),
                        Account.username.ilike(term),
                    )
                )
            total = query.count()
            sorts = {
                "fetched_at_desc": (PermissionSnapshot.fetched_at.desc(), PermissionSnapshot.id.desc()),
                "fetched_at_asc": (PermissionSnapshot.fetched_at.asc(), PermissionSnapshot.id.asc()),
                "expires_at_desc": (PermissionSnapshot.expires_at.desc(), PermissionSnapshot.id.desc()),
                "expires_at_asc": (PermissionSnapshot.expires_at.asc(), PermissionSnapshot.id.asc()),
            }
            rows = query.order_by(*sorts[sort]).offset(offset).limit(limit).all()
            catalog = _catalog_map(db)
            values = [
                AuthorizationSnapshot(
                    user_id=row.account_id,
                    display_name=username,
                    external_user_id=row.external_user_id,
                    app_key=row.app_key,
                    grant_count=len(normalize_grants(row.grants, catalog)),
                    grant_version=row.grant_version,
                    catalog_version=row.catalog_version,
                    snapshot_version=row.snapshot_version,
                    role_groups=_role_groups(row.groups),
                    fetched_at=row.fetched_at,
                    expires_at=row.expires_at,
                    expired=_utc(row.expires_at) <= now,
                )
                for row, username in rows
            ]
        return values, total

    def refresh_snapshot(self, user_id: uuid.UUID, *, actor_id: str) -> AuthorizationSnapshot:
        result = AuthorizationSnapshot.model_validate(refresh_account_snapshot(user_id).model_dump(by_alias=True))
        record_platform_audit(
            actor_id,
            "authz_integration.snapshot.refresh",
            None,
            {
                "snapshotVersion": result.snapshot_version,
                "grantCount": result.grant_count,
            },
        )
        return result

    def refresh_snapshot_for_external_user(self, external_user_id: str, expected_snapshot_version: str) -> None:
        authz_snapshot.refresh_snapshot_for_external_user(external_user_id, expected_snapshot_version)

    def invalidate_app_snapshots(self, app_key: str, catalog_version: int) -> None:
        authz_snapshot.invalidate_app_snapshots(app_key, catalog_version)

    def manifest(self, *, schema_version: int | None, actor_id: str) -> dict:
        if schema_version not in {None, FRAMEWORK_MANIFEST.schema_version}:
            raise AuthError(422, "unsupported manifest schema version")
        result = _current_manifest()
        record_platform_audit(
            actor_id,
            "authz_integration.manifest.export",
            None,
            {
                "schemaVersion": result["schema_version"],
                "permissionCount": len(result["permissions"]),
            },
        )
        return result

    def my_grants(self, *, actor_id: str) -> list[MyGrantResponse]:
        return _my_grants(actor_id=actor_id)

    def list_descriptor_keys(self) -> list[DescriptorKeyResponse]:
        return [DescriptorKeyResponse.model_validate(item) for item in _list_descriptor_keys()]

    def create_descriptor_key(
        self, payload: DescriptorKeyCreateRequest, *, actor_id: str
    ) -> DescriptorKeyCreateResponse:
        return _create_descriptor_key(payload, actor_id=actor_id)

    def update_descriptor_key(
        self, key_id: uuid.UUID, payload: DescriptorKeyUpdateRequest, *, actor_id: str
    ) -> DescriptorKeyResponse:
        return DescriptorKeyResponse.model_validate(_update_descriptor_key(key_id, payload, actor_id=actor_id))

    def delete_descriptor_key(self, key_id: uuid.UUID, *, actor_id: str) -> None:
        _delete_descriptor_key(key_id, actor_id=actor_id)


def _permission_dependency(code: str):
    def dependency() -> None:
        require_permission(code)

    return dependency


router = create_authorization_operations_router(
    BlankAuthorizationOperations(),
    current_user_dependency=_current_user,
    permission_dependency_factory=_permission_dependency,
)

__all__ = [
    "ALL_PERMISSIONS",
    "APIRouter",
    "Account",
    "AuthError",
    "AuthorizationCatalogItem",
    "AuthorizationConnectionResult",
    "AuthorizationSettings",
    "AuthorizationSettingsUpdate",
    "AuthorizationSnapshot",
    "AuthorizationStatus",
    "AuthzConnectionResponse",
    "AuthzStatusResponse",
    "BlankAuthorizationOperations",
    "BlankIntegrationAdapter",
    "CatalogItemResponse",
    "CatalogPermission",
    "ConnectionErrorResponse",
    "ConnectionTestResult",
    "CountStatsResponse",
    "CurrentUser",
    "DataScope",
    "DescriptorKey",
    "DescriptorKeyCreateRequest",
    "DescriptorKeyCreateResponse",
    "DescriptorKeyResponse",
    "DescriptorKeyUpdateRequest",
    "EasyAuthClientError",
    "EasyAuthForbiddenError",
    "EasyAuthPermissionClient",
    "EasyAuthSettingsUpdate",
    "EasyAuthStatus",
    "EasyAuthSummaryResponse",
    "FRAMEWORK_MANIFEST",
    "FRAMEWORK_PERMISSIONS",
    "Field",
    "HTTPException",
    "JSONResponse",
    "MyGrantResponse",
    "PermissionCatalog",
    "PermissionManifestRegistration",
    "PermissionManifestRegistry",
    "PermissionSnapshot",
    "PlatformModel",
    "PlatformSetting",
    "PrincipalSummaryResponse",
    "PrincipalValidationError",
    "Request",
    "RiskLevel",
    "SecretConfigurationError",
    "SessionLocal",
    "SnapshotResponse",
    "SnapshotStatsResponse",
    "UTC",
    "account_adapter",
    "annotations",
    "classify_connection_failure",
    "create_authorization_operations_router",
    "datetime",
    "decrypt_secret",
    "descriptor_router",
    "easyauth_descriptor",
    "ensure_account_snapshot",
    "hashlib",
    "httpx",
    "is_unsafe_bootstrap_secret",
    "normalize_catalog_risk_level",
    "normalize_grants",
    "or_",
    "os",
    "parse_upstream_principal_from_headers",
    "pg_insert",
    "record_platform_audit",
    "refresh_account_snapshot",
    "require_permission",
    "resolve_trusted_principal",
    "router",
    "secrets",
    "seed_platform_catalog",
    "snapshot_grants_for_account",
    "test_easyauth_connection",
    "uuid",
    "validate_principal_config",
]
