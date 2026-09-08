"""EasyAuth 目录最低版本：按 authority 作用域存储，GREATEST 原子抬升。"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm.attributes import flag_modified

from blank_app.database import SessionLocal
from blank_app.models import PlatformSetting

CATALOG_FLOOR_SETTING_KEY = "easyauth_catalog_floor"


def configured_authority(db) -> str:
    setting = db.get(PlatformSetting, "easyauth")
    raw = (setting.value or {}).get("base_url") if setting else ""
    return str(raw or "").rstrip("/")


def catalog_floor(db, app_key: str) -> int:
    setting = db.get(PlatformSetting, CATALOG_FLOOR_SETTING_KEY)
    return floor_from_value(setting.value if setting else None, configured_authority(db), app_key)


def floor_from_value(value: object, authority: str, app_key: str) -> int:
    if not isinstance(value, dict):
        return 0
    scoped = value.get(authority)
    if isinstance(scoped, dict):
        return _positive_int(scoped.get(app_key))
    if any(isinstance(item, dict) for item in value.values()):
        return 0
    return _positive_int(value.get(app_key))


def lock_catalog_floor(db, app_key: str) -> int:
    """锁住下限行后再读；无行则先插入空对象。"""

    db.execute(
        pg_insert(PlatformSetting)
        .values(key=CATALOG_FLOOR_SETTING_KEY, value={})
        .on_conflict_do_nothing(index_elements=["key"])
    )
    (db.query(PlatformSetting).filter(PlatformSetting.key == CATALOG_FLOOR_SETTING_KEY).with_for_update().one())
    return catalog_floor(db, app_key)


def raise_catalog_floor(db, app_key: str, catalog_version: int) -> bool:
    """已记录的最低版本 >= 宣布版本时返回 False(不改库);否则 GREATEST 写入并返回 True。"""

    current = lock_catalog_floor(db, app_key)
    raised = max(current, catalog_version)
    if raised == current:
        return False
    setting = db.get(PlatformSetting, CATALOG_FLOOR_SETTING_KEY)
    setting.value = _with_floor(setting.value, configured_authority(db), app_key, raised)
    flag_modified(setting, "value")
    return True


def reset_catalog_floor() -> None:
    with SessionLocal() as db:
        setting = db.get(PlatformSetting, CATALOG_FLOOR_SETTING_KEY)
        if setting is None:
            return
        db.delete(setting)
        db.commit()


def _with_floor(value: object, authority: str, app_key: str, catalog_version: int) -> dict:
    floors = dict(value) if isinstance(value, dict) else {}
    scoped_raw = floors.get(authority)
    scoped = dict(scoped_raw) if isinstance(scoped_raw, dict) else {}
    scoped[app_key] = catalog_version
    floors[authority] = scoped
    return floors


def _positive_int(raw: object) -> int:
    return raw if isinstance(raw, int) and raw > 0 else 0
