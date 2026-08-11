"""blank host 的 EasyAuth catalog、manifest 与 snapshot 持久化适配。"""

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


class SnapshotResponse(PlatformModel):
    user_id: uuid.UUID
    display_name: str
    external_user_id: str
    app_key: str
    grant_count: int
    grant_version: int
    catalog_version: int
    snapshot_version: str
    fetched_at: datetime
    expires_at: datetime
    expired: bool
    role_groups: list[str] = Field(default_factory=list)


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


def seed_platform_catalog() -> None:
    names = {
        "auth.totp.create": ("启用两步认证", "Enable two-factor authentication"),
        "auth.totp.advance": ("管理两步认证", "Manage two-factor authentication"),
        "auth.passkey.view": ("查看通行密钥", "View passkeys"),
        "auth.passkey.create": ("管理通行密钥", "Manage passkeys"),
        "accounts.local.view": ("查看本地账户", "View local accounts"),
        "accounts.local.manage": ("管理本地账户", "Manage local accounts"),
    }
    with SessionLocal() as db:
        registered_codes = {permission.code for permission in FRAMEWORK_PERMISSIONS}
        db.query(PermissionCatalog).filter(PermissionCatalog.code.notin_(registered_codes)).update(
            {PermissionCatalog.active: False},
            synchronize_session=False,
        )
        for permission in FRAMEWORK_PERMISSIONS:
            row = db.get(PermissionCatalog, permission.code)
            name_zh, name_en = names.get(permission.code, (permission.code, permission.code))
            if row is None:
                row = PermissionCatalog(
                    code=permission.code,
                    name_zh=name_zh,
                    name_en=name_en,
                    domain=permission.domain,
                    resource=permission.resource,
                    group_key=permission.group_key,
                    supported_scopes=[scope.value for scope in permission.supported_scopes],
                    risk_level=permission.risk_level,
                    active=permission.active,
                )
                db.add(row)
            else:
                row.name_zh = name_zh
                row.name_en = name_en
                row.domain = permission.domain
                row.resource = permission.resource
                row.group_key = permission.group_key
                row.supported_scopes = [scope.value for scope in permission.supported_scopes]
                row.risk_level = permission.risk_level
                row.active = permission.active
        db.commit()


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
    ensure_account_snapshot(account_id)
    return account_id


def _current_user() -> CurrentUser:
    user = account_adapter.current_user()
    if user.must_change_password:
        raise AuthError(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
    return user


def _permission_client() -> EasyAuthPermissionClient:
    data = _get_setting("easyauth")
    try:
        credential = decrypt_secret(str(data.get("credential") or ""))
    except SecretConfigurationError as exc:
        raise EasyAuthClientError("EasyAuth credential storage is not configured") from exc
    return EasyAuthPermissionClient(
        base_url=str(data.get("base_url") or ""),
        app_key=str(data.get("app_key") or FRAMEWORK_MANIFEST.app_key),
        auth_mode=str(data.get("auth_mode") or "static_app_token"),
        credential=credential,
        timeout=5,
    )


def _catalog_map(db) -> dict[str, CatalogPermission]:
    return {
        row.code: CatalogPermission(
            code=row.code,
            supported_scopes=frozenset(DataScope(scope) for scope in row.supported_scopes),
            active=row.active,
        )
        for row in db.query(PermissionCatalog).all()
    }


def test_easyauth_connection() -> ConnectionTestResult:
    return BlankIntegrationAdapter().test_easyauth()


descriptor_router = APIRouter()


def _current_manifest() -> dict:
    """生成与 EasyAuth app SDK 0.3 descriptor 兼容的当前 manifest。"""

    configured_app_key = str(_get_setting("easyauth").get("app_key") or FRAMEWORK_MANIFEST.app_key)
    with SessionLocal() as db:
        catalog = {row.code: row for row in db.query(PermissionCatalog).all()}
    scopes = sorted(
        {scope.value for permission in FRAMEWORK_MANIFEST.permissions for scope in permission.supported_scopes}
    )
    domains = sorted({permission.domain for permission in FRAMEWORK_MANIFEST.permissions})
    return {
        "schema_version": FRAMEWORK_MANIFEST.schema_version,
        "app": {
            "app_key": configured_app_key,
            "name": "Enterprise Blank",
            "description": "Reusable enterprise application framework",
            "is_active": True,
        },
        "scopes": [{"key": scope, "name": scope, "name_en": scope} for scope in scopes],
        "permission_groups": [
            {"key": domain, "name": domain, "name_en": domain, "parent_key": None} for domain in domains
        ],
        "permissions": [
            {
                "key": permission.code,
                "name": catalog[permission.code].name_zh if permission.code in catalog else permission.code,
                "name_en": catalog[permission.code].name_en if permission.code in catalog else permission.code,
                "group_key": permission.domain,
                "supported_scopes": [scope.value for scope in permission.supported_scopes],
                "risk_level": permission.risk_level,
                "is_active": permission.active,
            }
            for permission in FRAMEWORK_MANIFEST.permissions
        ],
        "authorization_groups": [],
        "approval_rules": [],
        "capabilities": ["directory", "notify"],
    }


def _validate_descriptor_token(token: str | None) -> bool:
    with SessionLocal() as db:
        active = db.query(DescriptorKey).filter(DescriptorKey.active.is_(True)).all()
        if not active:
            return os.getenv("BLANK_RUNTIME_ENV", "production").strip().lower() != "production"
        if not token:
            return False
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        matched = next((key for key in active if secrets.compare_digest(key.token_hash, token_hash)), None)
        if matched is None:
            return False
        matched.last_used_at = datetime.now(UTC)
        db.commit()
        return True


@descriptor_router.get("/.well-known/easyauth-app.json", include_in_schema=False)
def easyauth_descriptor(request: Request) -> JSONResponse:
    authorization = request.headers.get("authorization")
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :].strip() or None
    if not _validate_descriptor_token(token):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "descriptor_unauthorized", "message": "描述符访问未授权。"}},
        )
    manifest_payload = _current_manifest()
    return JSONResponse(
        {
            "descriptor_version": 1,
            "app": {
                "app_key": manifest_payload["app"]["app_key"],
                "name": manifest_payload["app"]["name"],
                "description": manifest_payload["app"]["description"],
            },
            "manifest": manifest_payload,
            "sdk": {"name": "easyauth-app-sdk-python", "version": "0.3.0"},
        }
    )


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


def _save_authorization_settings(payload: EasyAuthSettingsUpdate, *, actor_id: str) -> EasyAuthStatus:
    return BlankIntegrationAdapter().save_easyauth_settings(payload, actor_id=actor_id)


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


def ensure_account_snapshot(account_id: str | uuid.UUID) -> bool:
    """外部身份登录时补齐/刷新授权快照；失败保持零权限并允许后续重试。"""

    parsed_id = uuid.UUID(str(account_id))
    now = datetime.now(UTC)
    with SessionLocal() as db:
        account = db.get(Account, parsed_id)
        if account is None or not account.external_source or not account.external_user_id:
            return False
        setting = db.get(PlatformSetting, "easyauth")
        app_key = str((setting.value if setting else {}).get("app_key") or "")
        cached = (
            db.query(PermissionSnapshot)
            .filter(
                PermissionSnapshot.external_source == account.external_source,
                PermissionSnapshot.external_user_id == account.external_user_id,
                PermissionSnapshot.app_key == app_key,
            )
            .one_or_none()
        )
        if cached is not None and _utc(cached.expires_at) > now:
            return True
    try:
        refresh_account_snapshot(parsed_id)
    except HTTPException:
        return False
    return True


def refresh_account_snapshot(user_id: uuid.UUID) -> SnapshotResponse:
    """抓取并原子持久化单个外部账号快照；上游失败不覆盖最后成功数据。"""

    with SessionLocal() as db:
        account = db.get(Account, user_id)
        if account is None:
            raise HTTPException(404, "user not found")
        if not account.external_source or not account.external_user_id:
            raise HTTPException(409, "user external identity is not configured")
        client: object | None = None
        try:
            client = _permission_client()
            snapshot = client.fetch_permission_snapshot(account.external_user_id)
        except EasyAuthForbiddenError as exc:
            raise HTTPException(403, str(exc)) from exc
        except EasyAuthClientError as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            if client is not None:
                _close_permission_client(client)
        if _utc(snapshot.expires_at) <= datetime.now(UTC):
            raise HTTPException(503, "EasyAuth permission response is already expired")
        account_id = account.id
        display_name = account.username
        external_source = account.external_source
        external_user_id = account.external_user_id
        row = (
            db.query(PermissionSnapshot)
            .filter(
                PermissionSnapshot.external_source == external_source,
                PermissionSnapshot.external_user_id == external_user_id,
                PermissionSnapshot.app_key == snapshot.app_key,
            )
            .one_or_none()
        ) or PermissionSnapshot(
            account_id=account_id,
            external_source=external_source,
            external_user_id=external_user_id,
            app_key=snapshot.app_key,
        )
        values = {
            "account_id": account_id,
            "groups": [item.model_dump(mode="json") for item in snapshot.groups],
            "grants": [item.model_dump(mode="json") for item in snapshot.grants],
            "grant_version": snapshot.grant_version,
            "catalog_version": snapshot.catalog_version,
            "snapshot_version": snapshot.snapshot_version,
            "fetched_at": datetime.now(UTC),
            "expires_at": snapshot.expires_at,
        }
        row = _commit_snapshot_row(
            db,
            row,
            external_source=external_source,
            external_user_id=external_user_id,
            app_key=snapshot.app_key,
            values=values,
        )
        grant_count = len(normalize_grants(row.grants, _catalog_map(db)))
        return SnapshotResponse(
            user_id=account_id,
            display_name=display_name,
            external_user_id=external_user_id,
            app_key=row.app_key,
            grant_count=grant_count,
            grant_version=row.grant_version,
            catalog_version=row.catalog_version,
            snapshot_version=row.snapshot_version,
            fetched_at=row.fetched_at,
            expires_at=row.expires_at,
            expired=False,
            role_groups=_role_groups(row.groups),
        )


def _commit_snapshot_row(
    db,
    row: PermissionSnapshot,
    *,
    external_source: str,
    external_user_id: str,
    app_key: str,
    values: dict,
) -> PermissionSnapshot:
    """原子提交 snapshot；较旧 grant_version 永远不能覆盖较新的撤权结果。"""

    insert_values = {
        "id": row.id or uuid.uuid4(),
        "external_source": external_source,
        "external_user_id": external_user_id,
        "app_key": app_key,
        **values,
    }
    statement = pg_insert(PermissionSnapshot).values(**insert_values)
    update_fields = {
        field: getattr(statement.excluded, field)
        for field in (
            "account_id",
            "groups",
            "grants",
            "grant_version",
            "catalog_version",
            "snapshot_version",
            "fetched_at",
            "expires_at",
        )
    }
    statement = statement.on_conflict_do_update(
        constraint="uq_platform_permission_snapshot",
        set_=update_fields,
        where=statement.excluded.grant_version >= PermissionSnapshot.grant_version,
    )
    db.execute(statement)
    db.commit()
    row = (
        db.query(PermissionSnapshot)
        .filter(
            PermissionSnapshot.external_source == external_source,
            PermissionSnapshot.external_user_id == external_user_id,
            PermissionSnapshot.app_key == app_key,
        )
        .one()
    )
    db.refresh(row)
    return row


def _list_descriptor_keys() -> list[DescriptorKey]:
    with SessionLocal() as db:
        return db.query(DescriptorKey).order_by(DescriptorKey.created_at.desc()).all()


def _create_descriptor_key(payload: DescriptorKeyCreateRequest, *, actor_id: str) -> DescriptorKeyCreateResponse:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "密钥名称不能为空")
    token = f"epd_{secrets.token_urlsafe(32)}"
    with SessionLocal() as db:
        row = DescriptorKey(
            name=name,
            token_prefix=token[:10],
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            active=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        response = DescriptorKeyResponse.model_validate(row)
    record_platform_audit(actor_id, "authz.descriptor_key.create", None, {"id": response.id, "name": name})
    return DescriptorKeyCreateResponse(key=response, token=token)


def _update_descriptor_key(key_id: uuid.UUID, payload: DescriptorKeyUpdateRequest, *, actor_id: str) -> DescriptorKey:
    with SessionLocal() as db:
        row = db.get(DescriptorKey, key_id)
        if row is None:
            raise HTTPException(404, "描述符同步密钥不存在")
        before = {"active": row.active}
        row.active = payload.active
        db.commit()
        db.refresh(row)
        db.expunge(row)
    record_platform_audit(actor_id, "authz.descriptor_key.update", before, {"active": payload.active})
    return row


def _delete_descriptor_key(key_id: uuid.UUID, *, actor_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(DescriptorKey, key_id)
        if row is None:
            raise HTTPException(404, "描述符同步密钥不存在")
        before = {"name": row.name, "tokenPrefix": row.token_prefix, "active": row.active}
        db.delete(row)
        db.commit()
    record_platform_audit(actor_id, "authz.descriptor_key.delete", before, None)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _role_groups(groups: object) -> list[str]:
    names: list[str] = []
    for group in groups if isinstance(groups, list) else []:
        name = group.get("name") if isinstance(group, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _latency_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)


def _close_permission_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


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
        current = _get_setting("easyauth")
        base_url = payload.base_url if payload.base_url is not None else str(current.get("base_url") or "")
        app_key = payload.app_key if payload.app_key is not None else str(current.get("app_key") or "")
        if not base_url or not app_key:
            raise AuthError(422, "baseUrl and appKey are required for the blank host")
        saved = _save_authorization_settings(
            EasyAuthSettingsUpdate(
                base_url=base_url,
                app_key=app_key,
                credential=payload.credential,
                permission_request_url=payload.permission_request_url,
            ),
            actor_id=actor_id,
        )
        return AuthorizationSettings.model_validate(saved.model_dump(by_alias=True))

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
