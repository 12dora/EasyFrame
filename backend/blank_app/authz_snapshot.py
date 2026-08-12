"""blank host EasyAuth catalog 种子与授权快照持久化。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from blank_app.models import PermissionSnapshot
from enterprise_platform.authz import (
    CatalogPermission,
    EasyAuthPermissionClient,
)
from enterprise_platform.schemas import PlatformModel


def _facade():
    from blank_app import authz_api

    return authz_api


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


def seed_platform_catalog() -> None:
    names = {
        "auth.totp.create": ("启用两步认证", "Enable two-factor authentication"),
        "auth.totp.advance": ("管理两步认证", "Manage two-factor authentication"),
        "auth.passkey.view": ("查看通行密钥", "View passkeys"),
        "auth.passkey.create": ("管理通行密钥", "Manage passkeys"),
        "accounts.local.view": ("查看本地账户", "View local accounts"),
        "accounts.local.manage": ("管理本地账户", "Manage local accounts"),
    }
    with _facade().SessionLocal() as db:
        registered_codes = {permission.code for permission in _facade().FRAMEWORK_PERMISSIONS}
        db.query(_facade().PermissionCatalog).filter(_facade().PermissionCatalog.code.notin_(registered_codes)).update(
            {_facade().PermissionCatalog.active: False},
            synchronize_session=False,
        )
        for permission in _facade().FRAMEWORK_PERMISSIONS:
            row = db.get(_facade().PermissionCatalog, permission.code)
            name_zh, name_en = names.get(permission.code, (permission.code, permission.code))
            if row is None:
                row = _facade().PermissionCatalog(
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


def _permission_client() -> EasyAuthPermissionClient:
    data = _facade()._get_setting("easyauth")
    try:
        credential = _facade().decrypt_secret(str(data.get("credential") or ""))
    except _facade().SecretConfigurationError as exc:
        raise _facade().EasyAuthClientError("EasyAuth credential storage is not configured") from exc
    return _facade().EasyAuthPermissionClient(
        base_url=str(data.get("base_url") or ""),
        app_key=str(data.get("app_key") or _facade().FRAMEWORK_MANIFEST.app_key),
        auth_mode=str(data.get("auth_mode") or "static_app_token"),
        credential=credential,
        timeout=5,
    )


def _catalog_map(db) -> dict[str, CatalogPermission]:
    return {
        row.code: _facade().CatalogPermission(
            code=row.code,
            supported_scopes=frozenset(_facade().DataScope(scope) for scope in row.supported_scopes),
            active=row.active,
        )
        for row in db.query(_facade().PermissionCatalog).all()
    }


def ensure_account_snapshot(account_id: str | uuid.UUID) -> bool:
    """外部身份登录时补齐/刷新授权快照；失败保持零权限并允许后续重试。"""

    parsed_id = _facade().uuid.UUID(str(account_id))
    now = _facade().datetime.now(_facade().UTC)
    with _facade().SessionLocal() as db:
        account = db.get(_facade().Account, parsed_id)
        if account is None or not account.external_source or not account.external_user_id:
            return False
        setting = db.get(_facade().PlatformSetting, "easyauth")
        app_key = str((setting.value if setting else {}).get("app_key") or "")
        cached = (
            db.query(_facade().PermissionSnapshot)
            .filter(
                _facade().PermissionSnapshot.external_source == account.external_source,
                _facade().PermissionSnapshot.external_user_id == account.external_user_id,
                _facade().PermissionSnapshot.app_key == app_key,
            )
            .one_or_none()
        )
        if cached is not None and _facade()._utc(cached.expires_at) > now:
            return True
    try:
        _facade().refresh_account_snapshot(parsed_id)
    except _facade().HTTPException:
        return False
    return True


def refresh_account_snapshot(user_id: uuid.UUID) -> SnapshotResponse:
    """抓取并原子持久化单个外部账号快照；上游失败不覆盖最后成功数据。"""

    with _facade().SessionLocal() as db:
        account = db.get(_facade().Account, user_id)
        if account is None:
            raise _facade().HTTPException(404, "user not found")
        if not account.external_source or not account.external_user_id:
            raise _facade().HTTPException(409, "user external identity is not configured")
        client: object | None = None
        try:
            client = _facade()._permission_client()
            snapshot = client.fetch_permission_snapshot(account.external_user_id)
        except _facade().EasyAuthForbiddenError as exc:
            raise _facade().HTTPException(403, str(exc)) from exc
        except _facade().EasyAuthClientError as exc:
            raise _facade().HTTPException(503, str(exc)) from exc
        finally:
            if client is not None:
                _facade()._close_permission_client(client)
        if _facade()._utc(snapshot.expires_at) <= _facade().datetime.now(_facade().UTC):
            raise _facade().HTTPException(503, "EasyAuth permission response is already expired")
        account_id = account.id
        display_name = account.username
        external_source = account.external_source
        external_user_id = account.external_user_id
        row = (
            db.query(_facade().PermissionSnapshot)
            .filter(
                _facade().PermissionSnapshot.external_source == external_source,
                _facade().PermissionSnapshot.external_user_id == external_user_id,
                _facade().PermissionSnapshot.app_key == snapshot.app_key,
            )
            .one_or_none()
        ) or _facade().PermissionSnapshot(
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
            "fetched_at": _facade().datetime.now(_facade().UTC),
            "expires_at": snapshot.expires_at,
        }
        row = _facade()._commit_snapshot_row(
            db,
            row,
            external_source=external_source,
            external_user_id=external_user_id,
            app_key=snapshot.app_key,
            values=values,
        )
        grant_count = len(_facade().normalize_grants(row.grants, _facade()._catalog_map(db)))
        return _facade().SnapshotResponse(
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
            role_groups=_facade()._role_groups(row.groups),
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
        "id": row.id or _facade().uuid.uuid4(),
        "external_source": external_source,
        "external_user_id": external_user_id,
        "app_key": app_key,
        **values,
    }
    statement = _facade().pg_insert(_facade().PermissionSnapshot).values(**insert_values)
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
        where=statement.excluded.grant_version >= _facade().PermissionSnapshot.grant_version,
    )
    db.execute(statement)
    db.commit()
    row = (
        db.query(_facade().PermissionSnapshot)
        .filter(
            _facade().PermissionSnapshot.external_source == external_source,
            _facade().PermissionSnapshot.external_user_id == external_user_id,
            _facade().PermissionSnapshot.app_key == app_key,
        )
        .one()
    )
    db.refresh(row)
    return row


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=_facade().UTC)


def _role_groups(groups: object) -> list[str]:
    names: list[str] = []
    for group in groups if isinstance(groups, list) else []:
        name = group.get("name") if isinstance(group, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _close_permission_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()
