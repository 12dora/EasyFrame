"""blank 宿主把当前权限 manifest 推到 EasyAuth。

状态在 ``platform_settings.easyauth_manifest_sync``。凭据来自 ``easyauth`` 行,
解密方式与权限客户端相同;对外基址来自 ``oidc.frontend_base_url``(非 https 由推送器省略)。
启动时调度一次;保存 EasyAuth 设置后强制再推。
``BLANK_RUNTIME_ENV=test`` 或 pytest 进程里不启动线程。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from blank_app.database import SessionLocal
from blank_app.models import PlatformSetting
from enterprise_platform.easyauth import (
    STATIC_APP_TOKEN,
    ManifestSyncResult,
    ManifestSyncScheduler,
    ManifestSyncTarget,
    sync_manifest,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret

logger = logging.getLogger(__name__)

SETTING_KEY = "easyauth_manifest_sync"
_EASYAUTH_KEY = "easyauth"
_OIDC_KEY = "oidc"


class PlatformSettingsManifestStore:
    """``platform_settings`` 上的同步状态。读写都是短事务,不覆盖出站 HTTP。"""

    def load_state(self) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.get(PlatformSetting, SETTING_KEY)
            value = row.value if row is not None else None
            if not isinstance(value, dict) or not value:
                return None
            return dict(value)

    def save_state(self, state: dict[str, Any]) -> None:
        payload = dict(state)
        with SessionLocal() as db:
            row = _lock_sync_row(db)
            row.value = payload
            db.commit()


def load_manifest_sync_target() -> ManifestSyncTarget | None:
    """从已保存的权限集成设置组装目的地;缺配置或无法解密时返回 None。"""

    with SessionLocal() as db:
        easyauth = _setting(db, _EASYAUTH_KEY)
        frontend = str(_setting(db, _OIDC_KEY).get("frontend_base_url") or "")
    return _target_from_settings(easyauth, frontend)


def sync_blank_manifest(
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> ManifestSyncResult:
    """推送 ``_current_manifest()``。未配置时跳过,不抛给调用方。"""

    target = load_manifest_sync_target()
    if target is None:
        return ManifestSyncResult(skipped=True, pushed=False)
    return sync_manifest(
        target=target,
        manifest=_blank_manifest(),
        store=_STORE,
        force=force,
        transport=transport,
    )


def schedule_blank_manifest_sync(*, force: bool = False) -> None:
    """后台推送。测试进程或 ``BLANK_RUNTIME_ENV=test`` 时不启动线程。"""

    if _scheduler_disabled():
        return
    _SCHEDULER.schedule(force)


def _scheduler_disabled() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return os.getenv("BLANK_RUNTIME_ENV", "production").strip().lower() == "test"


def _run_scheduled(force: bool) -> None:
    sync_blank_manifest(force=force)


def _blank_manifest() -> dict[str, Any]:
    from blank_app.authz_descriptor import _current_manifest

    return _current_manifest()


def _lock_sync_row(db) -> PlatformSetting:
    """先 upsert 再 FOR UPDATE,避免并发首写插出两行;事务由调用方提交。"""

    db.execute(
        pg_insert(PlatformSetting).values(key=SETTING_KEY, value={}).on_conflict_do_nothing(index_elements=["key"])
    )
    db.flush()
    return db.query(PlatformSetting).filter(PlatformSetting.key == SETTING_KEY).with_for_update().one()


def _setting(db, key: str) -> dict[str, Any]:
    row = db.get(PlatformSetting, key)
    value = row.value if row is not None else None
    return dict(value) if isinstance(value, dict) else {}


def _target_from_settings(data: dict[str, Any], frontend: str) -> ManifestSyncTarget | None:
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    app_key = str(data.get("app_key") or "").strip()
    credential = str(data.get("credential") or "")
    if not _static_token_configured(base_url, app_key, credential, data.get("auth_mode")):
        return None
    token = _decrypt_token(credential)
    if not token:
        return None
    public = frontend.strip().rstrip("/") or None
    return ManifestSyncTarget(base_url, app_key, token, public)


def _static_token_configured(base_url: str, app_key: str, credential: str, auth_mode: object) -> bool:
    if not base_url or not app_key or not credential:
        logger.debug("EasyAuth 未配置,跳过 manifest 同步")
        return False
    if str(auth_mode or STATIC_APP_TOKEN) != STATIC_APP_TOKEN:
        logger.warning("EasyAuth auth_mode=%s 不支持 manifest 同步", auth_mode)
        return False
    return True


def _decrypt_token(raw: str) -> str:
    try:
        return decrypt_secret(raw)
    except SecretConfigurationError:
        logger.warning("EasyAuth 凭据无法解密,跳过 manifest 同步")
        return ""


_STORE = PlatformSettingsManifestStore()
_SCHEDULER = ManifestSyncScheduler(_run_scheduled)

__all__ = [
    "SETTING_KEY",
    "PlatformSettingsManifestStore",
    "load_manifest_sync_target",
    "schedule_blank_manifest_sync",
    "sync_blank_manifest",
]
