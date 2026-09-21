"""宿主性能:近静态行的进程级短 TTL 缓存(≤10s)。不缓存账号/快照/授权。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

from sqlalchemy import event

from blank_app.authz_catalog_floor import CATALOG_FLOOR_SETTING_KEY, floor_from_value
from blank_app.database import SessionLocal
from blank_app.models import PermissionCatalog, PlatformSetting
from enterprise_platform.authz import CatalogPermission, DataScope
from enterprise_platform.ttl_cache import TtlBox

T = TypeVar("T")

_TTL_SECONDS = 10.0
_WATCHERS_INSTALLED = False

_catalog_box: TtlBox[dict[str, CatalogPermission]] = TtlBox(_TTL_SECONDS)
_easyauth_box: TtlBox[dict[str, Any]] = TtlBox(_TTL_SECONDS)
_floor_box: TtlBox[tuple[str, object | None]] = TtlBox(_TTL_SECONDS)


def invalidate_catalog() -> None:
    _catalog_box.invalidate()


def invalidate_easyauth() -> None:
    _easyauth_box.invalidate()
    _floor_box.invalidate()


def invalidate_floors() -> None:
    _floor_box.invalidate()


def catalog_map(db=None) -> dict[str, CatalogPermission]:
    return _catalog_box.load(lambda: _load_catalog(db))


def cached_easyauth_setting(db=None) -> dict[str, Any]:
    return _easyauth_box.load(lambda: dict(_setting_value(db, "easyauth") or {}))


def cached_app_key(db=None) -> str:
    return str(cached_easyauth_setting(db).get("app_key") or "").strip()


def cached_catalog_floor(db, app_key: str) -> int:
    authority, value = _floor_box.load(lambda: _floor_payload(db))
    return floor_from_value(value, authority, app_key)


def persist_easyauth_setting(data: dict[str, Any], *, actor_id: str, action: str) -> None:
    from blank_app.adapter_support import _save_setting
    from blank_app.easyauth_manifest_sync import schedule_blank_manifest_sync

    _save_setting("easyauth", data, actor_id=actor_id, action=action)
    invalidate_easyauth()
    # 设置已提交后再推;BLANK_RUNTIME_ENV=test 时调度函数直接返回。
    schedule_blank_manifest_sync(force=True)


def install_static_cache_watchers() -> None:
    """ORM 写入权限目录 / easyauth / 目录下限时立刻失效。"""

    global _WATCHERS_INSTALLED
    if _WATCHERS_INSTALLED:
        return
    event.listen(PermissionCatalog, "after_insert", _on_catalog_write)
    event.listen(PermissionCatalog, "after_update", _on_catalog_write)
    event.listen(PermissionCatalog, "after_delete", _on_catalog_write)
    event.listen(PlatformSetting, "after_insert", _on_setting_write)
    event.listen(PlatformSetting, "after_update", _on_setting_write)
    event.listen(PlatformSetting, "after_delete", _on_setting_write)
    _WATCHERS_INSTALLED = True


def _on_catalog_write(*_args: object, **_kwargs: object) -> None:
    invalidate_catalog()


def _on_setting_write(_mapper, _connection, target) -> None:
    key = getattr(target, "key", "")
    if key == "easyauth":
        invalidate_easyauth()
    elif key == CATALOG_FLOOR_SETTING_KEY:
        invalidate_floors()


def _load_catalog(db) -> dict[str, CatalogPermission]:
    def load(session) -> dict[str, CatalogPermission]:
        return {row.code: _catalog_permission(row) for row in session.query(PermissionCatalog).all()}

    return _with_session(db, load)


def _catalog_permission(row: PermissionCatalog) -> CatalogPermission:
    scopes: set[DataScope] = set()
    raw_scopes = row.supported_scopes if isinstance(row.supported_scopes, list | tuple) else ()
    for raw_scope in raw_scopes:
        try:
            scopes.add(DataScope(str(raw_scope)))
        except ValueError:
            continue
    return CatalogPermission(code=row.code, supported_scopes=frozenset(scopes), active=row.active)


def _setting_value(db, key: str) -> object | None:
    return _with_session(db, lambda session: _copied_setting(session, key))


def _floor_payload(db) -> tuple[str, object | None]:
    return _authority_from_easyauth(db), _setting_value(db, CATALOG_FLOOR_SETTING_KEY)


def _authority_from_easyauth(db) -> str:
    data = cached_easyauth_setting(db)
    return str(data.get("base_url") or "").rstrip("/")


def _copied_setting(session, key: str) -> object | None:
    row = session.get(PlatformSetting, key)
    return None if row is None or row.value is None else deepcopy(row.value)


def _with_session(db, loader: Callable):
    close = False
    session = db
    if session is None:
        session = SessionLocal()
        close = True
    try:
        return loader(session)
    finally:
        if close:
            session.close()


install_static_cache_watchers()
