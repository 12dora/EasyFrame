"""blank host EasyAuth catalog 种子与授权快照持久化。"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import tuple_

from blank_app.models import PermissionSnapshot
from enterprise_platform.authz import (
    CatalogPermission,
    DataScope,
    EasyAuthPermissionClient,
    NormalizedGrant,
)
from enterprise_platform.schemas import PlatformModel

logger = logging.getLogger(__name__)

CATALOG_FLOOR_SETTING_KEY = "easyauth_catalog_floor"
SNAPSHOT_PULL_BACKOFF = timedelta(seconds=30)
_PULL_LOCK_COUNT = 64
_PULL_LOCKS = tuple(threading.Lock() for _ in range(_PULL_LOCK_COUNT))
_MAX_FAILURE_CACHE = 1024
_failed_pulls: dict[str, datetime] = {}
_failed_lock = threading.Lock()


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


def ensure_account_snapshot(account_id: str | uuid.UUID, *, force: bool = False) -> bool:
    """登录可 force 拉取;失败保留最后成功行。无缓存且失败则零权限。"""

    parsed_id = _facade().uuid.UUID(str(account_id))
    cached = _load_snapshot_cache(parsed_id)
    if cached is None:
        return False
    has_row, fresh, pull_key = cached
    if fresh and not force:
        return True
    if not force and _in_failure_backoff(pull_key):
        return has_row
    with _lock_for(pull_key):
        return _refresh_under_lock(parsed_id, force=force, pull_key=pull_key)


def _refresh_under_lock(account_id: uuid.UUID, *, force: bool, pull_key: str) -> bool:
    cached = _load_snapshot_cache(account_id)
    if cached is None:
        return False
    has_row, fresh, _ = cached
    if fresh and not force:
        return True
    if not force and _in_failure_backoff(pull_key):
        return has_row
    try:
        _facade().refresh_account_snapshot(account_id)
    except _facade().HTTPException:
        _remember_pull_failure(pull_key)
        if has_row:
            logger.warning("easyauth snapshot refresh failed; keeping last good row")
            return True
        return False
    _clear_pull_failure(pull_key)
    return True


def _load_snapshot_cache(account_id: uuid.UUID) -> tuple[bool, bool, str] | None:
    """无外部身份返回 None;否则 (是否已有行, 行是否未过期, 拉取协调键)。"""

    now = _facade().datetime.now(_facade().UTC)
    with _facade().SessionLocal() as db:
        account = db.get(_facade().Account, account_id)
        if account is None or not account.external_source or not account.external_user_id:
            return None
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
        pull_key = f"{app_key}:{account.external_user_id}"
        if cached is None:
            return False, False, pull_key
        return True, _facade()._utc(cached.expires_at) > now, pull_key


def _lock_for(pull_key: str) -> threading.Lock:
    return _PULL_LOCKS[hash(pull_key) % _PULL_LOCK_COUNT]


def _in_failure_backoff(pull_key: str) -> bool:
    now = _facade().datetime.now(_facade().UTC)
    with _failed_lock:
        _prune_failed_pulls(now)
        failed_at = _failed_pulls.get(pull_key)
        return failed_at is not None and now - failed_at < SNAPSHOT_PULL_BACKOFF


def _remember_pull_failure(pull_key: str) -> None:
    now = _facade().datetime.now(_facade().UTC)
    with _failed_lock:
        _prune_failed_pulls(now)
        _failed_pulls[pull_key] = now


def _clear_pull_failure(pull_key: str) -> None:
    with _failed_lock:
        _failed_pulls.pop(pull_key, None)


def _prune_failed_pulls(now: datetime) -> None:
    expired = [key for key, failed_at in _failed_pulls.items() if now - failed_at >= SNAPSHOT_PULL_BACKOFF]
    for key in expired:
        del _failed_pulls[key]
    while len(_failed_pulls) >= _MAX_FAILURE_CACHE:
        _failed_pulls.pop(next(iter(_failed_pulls)))


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
    """原子提交 snapshot；较旧 (grant_version, catalog_version) 不能覆盖较新结果。"""

    _reject_below_catalog_floor(db, app_key, values["catalog_version"])
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
        where=_newer_or_equal_snapshot(statement),
    )
    db.execute(statement)
    db.commit()
    return (
        db.query(_facade().PermissionSnapshot)
        .filter(
            _facade().PermissionSnapshot.external_source == external_source,
            _facade().PermissionSnapshot.external_user_id == external_user_id,
            _facade().PermissionSnapshot.app_key == app_key,
        )
        .one()
    )


def _newer_or_equal_snapshot(statement):
    """目录-only 变更也会落库;较旧 (grant_version, catalog_version) 不能覆盖。"""

    return tuple_(
        statement.excluded.grant_version,
        statement.excluded.catalog_version,
    ) >= tuple_(
        _facade().PermissionSnapshot.grant_version,
        _facade().PermissionSnapshot.catalog_version,
    )


def snapshot_grants_for_account(account) -> tuple[NormalizedGrant, ...]:
    """权限检查:过期则懒刷新;刷新失败 fail-closed 为零权限。"""

    if not account.external_source or not account.external_user_id:
        return ()
    _facade().ensure_account_snapshot(account.id, force=False)
    with _facade().SessionLocal() as db:
        integration = db.get(_facade().PlatformSetting, "easyauth")
        app_key = str((integration.value if integration else {}).get("app_key") or "")
        snapshot = (
            db.query(_facade().PermissionSnapshot)
            .filter(
                _facade().PermissionSnapshot.external_source == account.external_source,
                _facade().PermissionSnapshot.external_user_id == account.external_user_id,
                _facade().PermissionSnapshot.app_key == app_key,
            )
            .one_or_none()
        )
        if snapshot is None or _facade()._utc(snapshot.expires_at) <= _facade().datetime.now(_facade().UTC):
            return ()
        return tuple(
            NormalizedGrant(code=grant.code, scope=grant.scope)
            for grant in _facade().normalize_grants(snapshot.grants, _grant_catalog(db))
        )


def _grant_catalog(db) -> dict[str, CatalogPermission]:
    catalog: dict[str, CatalogPermission] = {}
    for row in db.query(_facade().PermissionCatalog).all():
        scopes: set[DataScope] = set()
        raw_scopes = row.supported_scopes if isinstance(row.supported_scopes, list | tuple) else ()
        for raw_scope in raw_scopes:
            try:
                scopes.add(DataScope(str(raw_scope)))
            except ValueError:
                continue
        catalog[row.code] = CatalogPermission(
            code=row.code,
            supported_scopes=frozenset(scopes),
            active=row.active,
        )
    return catalog


def refresh_snapshot_for_external_user(external_user_id: str, expected_snapshot_version: str) -> None:
    """webhook grant.changed:已持有 expected 版本则幂等成功,否则强制拉取。"""

    account, stored_version = _external_snapshot_state(external_user_id)
    if account is None:
        return
    if stored_version == expected_snapshot_version:
        return
    try:
        _facade().refresh_account_snapshot(account.id)
    except _facade().HTTPException:
        _, again = _external_snapshot_state(external_user_id)
        if again == expected_snapshot_version:
            return
        raise


def invalidate_app_snapshots(app_key: str, catalog_version: int) -> None:
    """webhook catalog.changed:抬高最低目录版本并过期缓存;已达该版本则幂等跳过。"""

    now = _facade().datetime.now(_facade().UTC)
    with _facade().SessionLocal() as db:
        if not _raise_catalog_floor(db, app_key, catalog_version):
            return
        db.query(_facade().PermissionSnapshot).filter(_facade().PermissionSnapshot.app_key == app_key).update(
            {_facade().PermissionSnapshot.expires_at: now},
            synchronize_session=False,
        )
        db.commit()


def _catalog_floor(db, app_key: str) -> int:
    setting = db.get(_facade().PlatformSetting, CATALOG_FLOOR_SETTING_KEY)
    raw = (setting.value or {}).get(app_key, 0) if setting else 0
    return raw if isinstance(raw, int) and raw > 0 else 0


def _raise_catalog_floor(db, app_key: str, catalog_version: int) -> bool:
    """已记录的最低版本 >= 宣布版本时返回 False(不改库);否则写入并返回 True。"""

    setting = db.get(_facade().PlatformSetting, CATALOG_FLOOR_SETTING_KEY)
    floors = dict(setting.value) if setting and setting.value else {}
    current = floors.get(app_key, 0)
    current = current if isinstance(current, int) else 0
    if current >= catalog_version:
        return False
    floors[app_key] = catalog_version
    if setting is None:
        db.add(_facade().PlatformSetting(key=CATALOG_FLOOR_SETTING_KEY, value=floors))
    else:
        setting.value = floors
    return True


def _reject_below_catalog_floor(db, app_key: str, catalog_version: int) -> None:
    if catalog_version < _catalog_floor(db, app_key):
        raise _facade().HTTPException(409, "snapshot catalog_version is below announced minimum")


def _external_snapshot_state(external_user_id: str):
    with _facade().SessionLocal() as db:
        account = (
            db.query(_facade().Account)
            .filter(
                _facade().Account.external_source == "authentik",
                _facade().Account.external_user_id == external_user_id,
            )
            .one_or_none()
        )
        if account is None or not account.external_source:
            return None, None
        setting = db.get(_facade().PlatformSetting, "easyauth")
        app_key = str((setting.value if setting else {}).get("app_key") or "")
        snapshot = (
            db.query(_facade().PermissionSnapshot)
            .filter(
                _facade().PermissionSnapshot.external_source == account.external_source,
                _facade().PermissionSnapshot.external_user_id == account.external_user_id,
                _facade().PermissionSnapshot.app_key == app_key,
            )
            .one_or_none()
        )
        db.expunge(account)
        return account, None if snapshot is None else snapshot.snapshot_version


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
