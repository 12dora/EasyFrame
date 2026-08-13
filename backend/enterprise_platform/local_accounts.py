"""本地账户 v2 管理契约、守卫与审计语义。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field, field_validator

from enterprise_platform.auth import AuthError, is_unsafe_bootstrap_secret
from enterprise_platform.authz import RiskLevel, normalize_catalog_risk_level
from enterprise_platform.schemas import CurrentUser, PasswordValue, PlatformModel, StrictPlatformModel

LOCAL_ACCOUNTS_VIEW = "accounts.local.view"
LOCAL_ACCOUNTS_MANAGE = "accounts.local.manage"
LOCAL_GRANTABLE_SCOPES = {"SELF", "ALL"}
BASELINE_SELF_SERVICE = {
    "auth.totp.create",
    "auth.totp.advance",
    "auth.passkey.view",
    "auth.passkey.create",
    "notification.center.view",
}


class LocalGrant(StrictPlatformModel):
    code: str = Field(min_length=1, max_length=160)
    scope: str = Field(min_length=1, max_length=32)


class LocalAccountSummary(PlatformModel):
    id: uuid.UUID
    username: str
    email: str | None = None
    active: bool
    is_admin: bool
    totp_enabled: bool
    passkey_count: int
    must_change_password: bool
    permission_count: int
    expires_at: datetime | None = None
    expired: bool
    created_at: datetime


class LocalAccountDetail(LocalAccountSummary):
    ui_locale: str
    permissions: list[LocalGrant] = Field(default_factory=list)
    baseline_permissions: list[str] = Field(default_factory=list)
    local_grants_version: int


class LocalAccountListMeta(PlatformModel):
    total: int


class LocalAccountListResponse(PlatformModel):
    data: list[LocalAccountSummary]
    meta: LocalAccountListMeta


class LocalAccountPermissionCatalogItem(PlatformModel):
    code: str
    name_zh: str
    name_en: str
    group_key: str
    risk_level: RiskLevel
    supported_scopes: list[str]
    grantable_scopes: list[str]


class LocalAccountPermissionCatalogResponse(PlatformModel):
    data: list[LocalAccountPermissionCatalogItem]


class CreateLocalAccountRequest(StrictPlatformModel):
    username: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    password: PasswordValue
    must_change_password: bool = True
    is_admin: bool = False
    permissions: list[LocalGrant] = Field(default_factory=list)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_timezone_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("有效期必须包含时区信息")
        return value


class UpdateLocalAccountRequest(StrictPlatformModel):
    email: str | None = Field(default=None, max_length=200)
    active: bool | None = None
    is_admin: bool | None = None
    ui_locale: str | None = Field(default=None, max_length=10)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_timezone_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("有效期必须包含时区信息")
        return value


class ResetLocalAccountPasswordRequest(StrictPlatformModel):
    password: PasswordValue
    must_change_password: bool = True


class SetLocalAccountPermissionsRequest(StrictPlatformModel):
    permissions: list[LocalGrant] = Field(default_factory=list)
    expected_version: int = Field(ge=0)


LocalAccountCreateRequest = CreateLocalAccountRequest
LocalAccountUpdateRequest = UpdateLocalAccountRequest
LocalAccountPasswordRequest = ResetLocalAccountPasswordRequest
LocalAccountPermissionsRequest = SetLocalAccountPermissionsRequest


@dataclass(frozen=True, slots=True)
class LocalAccountRecord:
    id: uuid.UUID
    username: str
    email: str | None
    active: bool
    is_admin: bool
    local_permissions: list[Any]
    local_grants_version: int
    expires_at: datetime | None
    must_change_password: bool
    ui_locale: str
    totp_enabled: bool
    totp_secret: str | None
    totp_pending_secret: str | None
    created_at: datetime
    passkey_count: int = 0


@dataclass(frozen=True, slots=True)
class LocalPermissionRecord:
    code: str
    name_zh: str
    name_en: str
    domain: str
    group_key: str | None
    supported_scopes: list[str]
    risk_level: object
    active: bool


class LocalAccountUnitOfWork(Protocol):
    def list_accounts(self, *, search: str | None) -> list[LocalAccountRecord]: ...
    def get_account(self, account_id: uuid.UUID, *, for_update: bool = False) -> LocalAccountRecord | None: ...
    def permission_catalog(self) -> list[LocalPermissionRecord]: ...
    def lock_local_admins(self) -> None: ...
    def usable_local_admin_count(self, *, now: datetime) -> int: ...
    def create_account(self, **values: Any) -> LocalAccountRecord: ...
    def update_account(self, account_id: uuid.UUID, **values: Any) -> LocalAccountRecord: ...
    def delete_account(self, account_id: uuid.UUID) -> None: ...
    def append_audit(
        self,
        *,
        actor_id: str,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None: ...


class LocalAccountAdminPort(Protocol):
    """宿主唯一职责：提供同一事务内的加锁读、写入与审计追加。"""

    def unit_of_work(self) -> AbstractContextManager[LocalAccountUnitOfWork]: ...


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _expired(value: datetime | None, *, now: datetime) -> bool:
    return value is not None and _utc(value) <= now


def _catalog_map(rows: list[LocalPermissionRecord]) -> dict[str, LocalPermissionRecord]:
    return {row.code: row for row in rows if row.active}


def _normalize_stored_grants(values: list[Any], catalog: dict[str, LocalPermissionRecord]) -> list[LocalGrant]:
    result: list[LocalGrant] = []
    seen: set[str] = set()
    for value in values or []:
        if not isinstance(value, dict):
            continue
        code, scope = value.get("code"), value.get("scope")
        row = catalog.get(code) if isinstance(code, str) else None
        if row is None or code in seen or not isinstance(scope, str):
            continue
        if scope not in set(row.supported_scopes) & LOCAL_GRANTABLE_SCOPES:
            continue
        seen.add(code)
        result.append(LocalGrant(code=code, scope=scope))
    return result


def _validate_grants(grants: list[LocalGrant], catalog: dict[str, LocalPermissionRecord]) -> list[LocalGrant]:
    seen: set[str] = set()
    result: list[LocalGrant] = []
    for grant in grants:
        if grant.code in seen:
            raise AuthError(422, "权限代码不能重复")
        seen.add(grant.code)
        row = catalog.get(grant.code)
        if row is None:
            raise AuthError(422, "权限代码无效")
        if grant.scope not in set(row.supported_scopes) & LOCAL_GRANTABLE_SCOPES:
            raise AuthError(422, "权限范围无效")
        if grant.code not in BASELINE_SELF_SERVICE:
            result.append(grant)
    return result


def _grant_dicts(grants: list[LocalGrant]) -> list[dict[str, str]]:
    return [grant.model_dump() for grant in grants]


def _grant_tuples(grants: list[LocalGrant]) -> set[tuple[str, str]]:
    return {(grant.code, grant.scope) for grant in grants}


def _high_codes(catalog: dict[str, LocalPermissionRecord]) -> set[str]:
    return {code for code, row in catalog.items() if normalize_catalog_risk_level(row.risk_level) == "high"}


def _touches_high(before: list[LocalGrant], after: list[LocalGrant], *, high_codes: set[str]) -> bool:
    difference = _grant_tuples(before) ^ _grant_tuples(after)
    return any(code in high_codes for code, _scope in difference)


def _is_privileged(record: LocalAccountRecord, grants: list[LocalGrant], *, high_codes: set[str]) -> bool:
    return record.is_admin or any(grant.code in high_codes for grant in grants)


def _account_state(record: LocalAccountRecord, grants: list[LocalGrant]) -> dict[str, Any]:
    return {
        "targetAccountId": str(record.id),
        "username": record.username,
        "email": record.email,
        "active": record.active,
        "isAdmin": record.is_admin,
        "uiLocale": record.ui_locale,
        "expiresAt": record.expires_at,
        "permissions": _grant_dicts(grants),
        "mustChangePassword": record.must_change_password,
        "totpEnabled": record.totp_enabled,
    }


def _is_self(record: LocalAccountRecord, actor_id: str) -> bool:
    try:
        return record.id == uuid.UUID(actor_id)
    except (TypeError, ValueError, AttributeError):
        return False


class _LocalAccountService:
    def __init__(self, port: LocalAccountAdminPort):
        self.port = port

    @staticmethod
    def _actor_id(user: CurrentUser) -> str:
        return user.account_id or user.id

    @staticmethod
    def _require_local(uow: LocalAccountUnitOfWork, account_id: uuid.UUID, *, lock: bool) -> LocalAccountRecord:
        account = uow.get_account(account_id, for_update=lock)
        if account is None:
            raise AuthError(404, "本地账户不存在")
        return account

    @staticmethod
    def _catalog(uow: LocalAccountUnitOfWork) -> tuple[list[LocalPermissionRecord], dict[str, LocalPermissionRecord]]:
        rows = uow.permission_catalog()
        return rows, _catalog_map(rows)

    @staticmethod
    def _guard_privileged_target(user: CurrentUser, privileged: bool) -> None:
        if privileged and not user.is_local_superadmin:
            raise AuthError(403, "无权修改特权账户")

    @staticmethod
    def _ensure_admin_remains(uow: LocalAccountUnitOfWork, *, now: datetime) -> None:
        if uow.usable_local_admin_count(now=now) < 1:
            raise AuthError(422, "必须保留至少一个可用的本地管理员")

    @staticmethod
    def _summary(
        account: LocalAccountRecord,
        *,
        catalog: dict[str, LocalPermissionRecord],
        now: datetime,
    ) -> LocalAccountSummary:
        grants = _normalize_stored_grants(account.local_permissions, catalog)
        return LocalAccountSummary(
            id=account.id,
            username=account.username,
            email=account.email,
            active=account.active,
            is_admin=account.is_admin,
            totp_enabled=account.totp_enabled,
            passkey_count=account.passkey_count,
            must_change_password=account.must_change_password,
            permission_count=len(grants),
            expires_at=account.expires_at,
            expired=_expired(account.expires_at, now=now),
            created_at=account.created_at,
        )

    def _detail(
        self,
        account: LocalAccountRecord,
        *,
        catalog: dict[str, LocalPermissionRecord],
        now: datetime,
    ) -> LocalAccountDetail:
        grants = _normalize_stored_grants(account.local_permissions, catalog)
        return LocalAccountDetail(
            **self._summary(account, catalog=catalog, now=now).model_dump(),
            ui_locale=account.ui_locale,
            permissions=grants,
            baseline_permissions=sorted(BASELINE_SELF_SERVICE),
            local_grants_version=account.local_grants_version,
        )

    def list(self, *, search: str | None) -> tuple[list[LocalAccountSummary], int]:
        now = datetime.now(UTC)
        with self.port.unit_of_work() as uow:
            _, catalog = self._catalog(uow)
            accounts = uow.list_accounts(search=search)
            return [self._summary(account, catalog=catalog, now=now) for account in accounts], len(accounts)

    def permission_catalog(self) -> list[LocalAccountPermissionCatalogItem]:
        with self.port.unit_of_work() as uow:
            rows = [row for row in uow.permission_catalog() if row.active]
        return [
            LocalAccountPermissionCatalogItem(
                code=row.code,
                name_zh=row.name_zh,
                name_en=row.name_en,
                group_key=row.group_key or row.domain,
                risk_level=normalize_catalog_risk_level(row.risk_level),
                supported_scopes=list(row.supported_scopes),
                grantable_scopes=[scope for scope in row.supported_scopes if scope in LOCAL_GRANTABLE_SCOPES],
            )
            for row in sorted(rows, key=lambda item: item.code)
        ]

    def create(self, payload: CreateLocalAccountRequest, *, user: CurrentUser) -> LocalAccountDetail:
        now = datetime.now(UTC)
        username = payload.username.strip()
        if not username:
            raise AuthError(422, "用户名不能为空")
        if payload.is_admin and payload.permissions:
            raise AuthError(422, "管理员账户不能携带本地权限")
        if payload.is_admin and not user.is_local_superadmin:
            raise AuthError(422, "委派管理员不能创建管理员账户")
        if payload.expires_at is not None and _expired(payload.expires_at, now=now):
            raise AuthError(422, "有效期必须晚于当前时间")
        if payload.is_admin and payload.expires_at is not None:
            raise AuthError(422, "管理员账户不能设置有效期")
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            _, catalog = self._catalog(uow)
            grants = _validate_grants(payload.permissions, catalog)
            if not user.is_local_superadmin and _touches_high([], grants, high_codes=_high_codes(catalog)):
                raise AuthError(422, "委派管理员不能变更高风险权限")
            account = uow.create_account(
                username=username,
                email=_normalized_email(payload.email),
                password=payload.password,
                active=True,
                is_admin=payload.is_admin,
                local_permissions=[] if payload.is_admin else _grant_dicts(grants),
                local_grants_version=0,
                expires_at=payload.expires_at,
                must_change_password=payload.must_change_password,
                ui_locale="zh-CN",
            )
            uow.append_audit(
                actor_id=self._actor_id(user),
                action="accounts.local.create",
                before=None,
                after={
                    "targetAccountId": str(account.id),
                    "changedKeys": [
                        "username",
                        "email",
                        "password",
                        "mustChangePassword",
                        "isAdmin",
                        "permissions",
                        "expiresAt",
                    ],
                },
            )
            self._ensure_admin_remains(uow, now=now)
            return self._detail(account, catalog=catalog, now=now)

    def get(self, account_id: uuid.UUID) -> LocalAccountDetail:
        now = datetime.now(UTC)
        with self.port.unit_of_work() as uow:
            account = self._require_local(uow, account_id, lock=False)
            _, catalog = self._catalog(uow)
            return self._detail(account, catalog=catalog, now=now)

    @staticmethod
    def _update_requests_change(
        payload: UpdateLocalAccountRequest, account: LocalAccountRecord, *, is_admin_change: bool
    ) -> bool:
        return (
            ("email" in payload.model_fields_set and _normalized_email(payload.email) != account.email)
            or (payload.ui_locale is not None and payload.ui_locale != account.ui_locale)
            or (payload.active is not None and payload.active != account.active)
            or is_admin_change
            or ("expires_at" in payload.model_fields_set and payload.expires_at != account.expires_at)
        )

    def _guard_update(
        self,
        payload: UpdateLocalAccountRequest,
        account: LocalAccountRecord,
        *,
        user: CurrentUser,
        catalog: dict[str, LocalPermissionRecord],
        before_grants: list[LocalGrant],
        is_admin_change: bool,
    ) -> None:
        if is_admin_change and not user.is_local_superadmin:
            raise AuthError(422, "委派管理员不能变更管理员身份")
        requested_change = self._update_requests_change(payload, account, is_admin_change=is_admin_change)
        if requested_change and not user.is_local_superadmin:
            self._guard_privileged_target(
                user,
                _is_privileged(account, before_grants, high_codes=_high_codes(catalog)),
            )

    @staticmethod
    def _update_profile_values(
        payload: UpdateLocalAccountRequest, account: LocalAccountRecord, *, actor_id: str
    ) -> tuple[dict[str, Any], list[str]]:
        values: dict[str, Any] = {}
        changed_keys: list[str] = []
        if "email" in payload.model_fields_set and _normalized_email(payload.email) != account.email:
            values["email"] = _normalized_email(payload.email)
            changed_keys.append("email")
        if payload.ui_locale is not None and payload.ui_locale != account.ui_locale:
            values["ui_locale"] = payload.ui_locale
            changed_keys.append("uiLocale")
        if payload.active is not None and payload.active != account.active:
            if _is_self(account, actor_id) and not payload.active:
                raise AuthError(403, "不能停用自己的账户")
            values["active"] = payload.active
            changed_keys.append("active")
        return values, changed_keys

    @staticmethod
    def _apply_admin_change(
        payload: UpdateLocalAccountRequest,
        account: LocalAccountRecord,
        *,
        actor_id: str,
        values: dict[str, Any],
        changed_keys: list[str],
    ) -> None:
        if _is_self(account, actor_id) and not payload.is_admin:
            raise AuthError(403, "不能撤销自己的管理员权限")
        values["is_admin"] = payload.is_admin
        changed_keys.append("isAdmin")
        if payload.is_admin:
            if "expires_at" in payload.model_fields_set and payload.expires_at is not None:
                raise AuthError(422, "管理员账户不能设置有效期")
            values["local_permissions"] = []
            values["local_grants_version"] = account.local_grants_version + 1
            if account.expires_at is not None:
                values["expires_at"] = None
                changed_keys.append("expiresAt")

    @staticmethod
    def _apply_expiry_change(
        payload: UpdateLocalAccountRequest,
        account: LocalAccountRecord,
        *,
        actor_id: str,
        now: datetime,
        values: dict[str, Any],
        changed_keys: list[str],
    ) -> None:
        if _is_self(account, actor_id):
            raise AuthError(403, "不能修改自己的有效期")
        if payload.expires_at is not None and _expired(payload.expires_at, now=now):
            raise AuthError(422, "有效期必须晚于当前时间")
        resulting_admin = values.get("is_admin", account.is_admin)
        if resulting_admin and payload.expires_at is not None:
            raise AuthError(422, "管理员账户不能设置有效期")
        if payload.expires_at != account.expires_at and "expires_at" not in values:
            values["expires_at"] = payload.expires_at
            changed_keys.append("expiresAt")

    def update(
        self, account_id: uuid.UUID, payload: UpdateLocalAccountRequest, *, user: CurrentUser
    ) -> LocalAccountDetail:
        now = datetime.now(UTC)
        actor_id = self._actor_id(user)
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            account = self._require_local(uow, account_id, lock=True)
            _, catalog = self._catalog(uow)
            before_grants = _normalize_stored_grants(account.local_permissions, catalog)
            is_admin_change = payload.is_admin is not None and payload.is_admin != account.is_admin
            self._guard_update(
                payload,
                account,
                user=user,
                catalog=catalog,
                before_grants=before_grants,
                is_admin_change=is_admin_change,
            )

            values, changed_keys = self._update_profile_values(payload, account, actor_id=actor_id)
            if is_admin_change:
                self._apply_admin_change(payload, account, actor_id=actor_id, values=values, changed_keys=changed_keys)
            if "expires_at" in payload.model_fields_set:
                self._apply_expiry_change(
                    payload, account, actor_id=actor_id, now=now, values=values, changed_keys=changed_keys
                )

            before = _account_state(account, before_grants)
            updated = uow.update_account(account.id, **values) if values else account
            self._ensure_admin_remains(uow, now=now)
            uow.append_audit(
                actor_id=actor_id,
                action="accounts.local.update",
                before=before,
                after={"targetAccountId": str(account.id), "changedKeys": changed_keys},
            )
            return self._detail(updated, catalog=catalog, now=now)

    def delete(self, account_id: uuid.UUID, *, user: CurrentUser) -> None:
        now = datetime.now(UTC)
        actor_id = self._actor_id(user)
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            account = self._require_local(uow, account_id, lock=True)
            _, catalog = self._catalog(uow)
            grants = _normalize_stored_grants(account.local_permissions, catalog)
            if _is_self(account, actor_id):
                raise AuthError(403, "不能删除自己的账户")
            self._guard_privileged_target(user, _is_privileged(account, grants, high_codes=_high_codes(catalog)))
            before = _account_state(account, grants)
            uow.delete_account(account.id)
            self._ensure_admin_remains(uow, now=now)
            uow.append_audit(
                actor_id=actor_id,
                action="accounts.local.delete",
                before=before,
                after={"targetAccountId": str(account.id), "changedKeys": ["deleted"]},
            )

    def reset_password(
        self, account_id: uuid.UUID, payload: ResetLocalAccountPasswordRequest, *, user: CurrentUser
    ) -> LocalAccountDetail:
        now = datetime.now(UTC)
        actor_id = self._actor_id(user)
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            account = self._require_local(uow, account_id, lock=True)
            _, catalog = self._catalog(uow)
            grants = _normalize_stored_grants(account.local_permissions, catalog)
            if _is_self(account, actor_id):
                raise AuthError(403, "管理面不能重置自己的密码")
            self._guard_privileged_target(user, _is_privileged(account, grants, high_codes=_high_codes(catalog)))
            # 超管被重置沿用 bootstrap 强度:长度、弱值与公共示例标记同一份黑名单(06 §2)。
            if account.is_admin and is_unsafe_bootstrap_secret(payload.password, min_length=12):
                raise AuthError(422, "管理员密码不符合强度要求")
            updated = uow.update_account(
                account.id,
                password=payload.password,
                must_change_password=payload.must_change_password,
                sessions_revoked_at=now,
            )
            uow.append_audit(
                actor_id=actor_id,
                action="accounts.local.password.reset",
                before={"targetAccountId": str(account.id)},
                after={
                    "targetAccountId": str(account.id),
                    "changedKeys": ["password", "mustChangePassword", "sessionsRevokedAt"],
                },
            )
            self._ensure_admin_remains(uow, now=now)
            return self._detail(updated, catalog=catalog, now=now)

    def set_permissions(
        self, account_id: uuid.UUID, payload: SetLocalAccountPermissionsRequest, *, user: CurrentUser
    ) -> LocalAccountDetail:
        now = datetime.now(UTC)
        actor_id = self._actor_id(user)
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            account = self._require_local(uow, account_id, lock=True)
            if payload.expected_version != account.local_grants_version:
                raise AuthError(409, "权限版本已过期")
            _, catalog = self._catalog(uow)
            before = _normalize_stored_grants(account.local_permissions, catalog)
            after = _validate_grants(payload.permissions, catalog)
            if account.is_admin:
                raise AuthError(422, "管理员账户不能写入本地权限")
            high_codes = _high_codes(catalog)
            if not user.is_local_superadmin and _touches_high(before, after, high_codes=high_codes):
                raise AuthError(422, "委派管理员不能变更高风险权限")
            changed = _grant_tuples(before) != _grant_tuples(after)
            if changed:
                self._guard_privileged_target(user, _is_privileged(account, before, high_codes=high_codes))
            removed = sorted(_grant_tuples(before) - _grant_tuples(after))
            added = sorted(_grant_tuples(after) - _grant_tuples(before))
            updated = (
                uow.update_account(
                    account.id,
                    local_permissions=_grant_dicts(after),
                    local_grants_version=account.local_grants_version + 1,
                )
                if changed
                else account
            )
            uow.append_audit(
                actor_id=actor_id,
                action="accounts.local.permissions.set",
                before={
                    "targetAccountId": str(account.id),
                    "permissions": [{"code": c, "scope": s} for c, s in removed],
                },
                after={
                    "targetAccountId": str(account.id),
                    "permissions": [{"code": c, "scope": s} for c, s in added],
                    "changedKeys": ["permissions"] if changed else [],
                },
            )
            self._ensure_admin_remains(uow, now=now)
            return self._detail(updated, catalog=catalog, now=now)

    def disable_totp(self, account_id: uuid.UUID, *, user: CurrentUser) -> LocalAccountDetail:
        now = datetime.now(UTC)
        actor_id = self._actor_id(user)
        with self.port.unit_of_work() as uow:
            uow.lock_local_admins()
            account = self._require_local(uow, account_id, lock=True)
            _, catalog = self._catalog(uow)
            grants = _normalize_stored_grants(account.local_permissions, catalog)
            if _is_self(account, actor_id):
                raise AuthError(403, "管理面不能救援自己的 TOTP")
            self._guard_privileged_target(user, _is_privileged(account, grants, high_codes=_high_codes(catalog)))
            configured = bool(account.totp_enabled or account.totp_secret or account.totp_pending_secret)
            updated = uow.update_account(
                account.id,
                totp_enabled=False,
                totp_secret=None,
                totp_pending_secret=None,
                sessions_revoked_at=now,
            )
            uow.append_audit(
                actor_id=actor_id,
                action="accounts.local.totp.disable",
                before={"targetAccountId": str(account.id), "totpEnabled": account.totp_enabled},
                after={
                    "targetAccountId": str(account.id),
                    "changedKeys": [*(["totpConfiguration"] if configured else []), "sessionsRevokedAt"],
                },
            )
            self._ensure_admin_remains(uow, now=now)
            return self._detail(updated, catalog=catalog, now=now)


def _normalized_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def create_local_accounts_router(
    operations: LocalAccountAdminPort,
    *,
    current_user_dependency: Callable[..., CurrentUser],
    permission_dependency_factory: Callable[[str], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(prefix="/local-accounts", tags=["local-accounts"])
    service = _LocalAccountService(operations)

    def port_call(callback):
        try:
            return callback()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def actor(user: CurrentUser = Depends(current_user_dependency)) -> CurrentUser:
        if user.must_change_password:
            raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
        return user

    @router.get("", response_model=LocalAccountListResponse)
    def list_local_accounts(
        search: str | None = Query(None, max_length=160),
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountListResponse:
        rows, total = port_call(lambda: service.list(search=search))
        return LocalAccountListResponse(data=rows, meta=LocalAccountListMeta(total=total))

    @router.post("", response_model=LocalAccountDetail, status_code=201)
    def create_local_account(
        body: CreateLocalAccountRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.create(body, user=user))

    @router.get("/permission-catalog", response_model=LocalAccountPermissionCatalogResponse)
    def get_local_account_permission_catalog(
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountPermissionCatalogResponse:
        return LocalAccountPermissionCatalogResponse(data=port_call(service.permission_catalog))

    @router.get("/{account_id}", response_model=LocalAccountDetail)
    def get_local_account(
        account_id: uuid.UUID,
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.get(account_id))

    @router.patch("/{account_id}", response_model=LocalAccountDetail)
    def update_local_account(
        account_id: uuid.UUID,
        body: UpdateLocalAccountRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.update(account_id, body, user=user))

    @router.delete("/{account_id}", status_code=204)
    def delete_local_account(
        account_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> Response:
        port_call(lambda: service.delete(account_id, user=user))
        return Response(status_code=204)

    @router.post("/{account_id}/password", response_model=LocalAccountDetail)
    def reset_local_account_password(
        account_id: uuid.UUID,
        body: ResetLocalAccountPasswordRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.reset_password(account_id, body, user=user))

    @router.put("/{account_id}/permissions", response_model=LocalAccountDetail)
    def set_local_account_permissions(
        account_id: uuid.UUID,
        body: SetLocalAccountPermissionsRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.set_permissions(account_id, body, user=user))

    @router.delete("/{account_id}/totp", response_model=LocalAccountDetail)
    def disable_local_account_totp(
        account_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: service.disable_totp(account_id, user=user))

    return router


create_local_account_admin_router = create_local_accounts_router
