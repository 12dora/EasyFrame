"""blank 宿主 manifest 推送:设置行、短事务状态与启动/保存触发。"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest

from blank_app.authz_catalog_floor import CATALOG_FLOOR_SETTING_KEY
from blank_app.database import SessionLocal
from blank_app.easyauth_manifest_sync import (
    SETTING_KEY,
    PlatformSettingsManifestStore,
    load_manifest_sync_target,
    schedule_blank_manifest_sync,
    sync_blank_manifest,
)
from blank_app.models import PlatformSetting
from enterprise_platform.easyauth import ManifestSyncTarget
from enterprise_platform.secrets import encrypt_secret

pytestmark = pytest.mark.usefixtures("blank_admin_seeded", "restored_settings")

_SNAPSHOT_KEYS = (SETTING_KEY, "easyauth", "oidc", CATALOG_FLOOR_SETTING_KEY)
_BASE = "https://auth.example.test"
_APP_KEY = "enterprise-blank"
_TOKEN = "eat_plain"


def _read(keys: tuple[str, ...]) -> dict[str, dict[str, Any] | None]:
    found: dict[str, dict[str, Any] | None] = {}
    with SessionLocal() as db:
        for key in keys:
            row = db.get(PlatformSetting, key)
            value = row.value if row is not None else None
            found[key] = dict(value) if isinstance(value, dict) else None
    return found


def _write(values: dict[str, dict[str, Any] | None]) -> None:
    with SessionLocal() as db:
        for key, value in values.items():
            row = db.get(PlatformSetting, key)
            if value is None:
                if row is not None:
                    db.delete(row)
                continue
            if row is None:
                db.add(PlatformSetting(key=key, value=dict(value)))
            else:
                row.value = dict(value)
        db.commit()


@pytest.fixture
def restored_settings(blank_admin_seeded: None) -> Any:
    del blank_admin_seeded
    previous = _read(_SNAPSHOT_KEYS)
    try:
        yield
    finally:
        _write(previous)


def _put_easyauth(*, token: str = _TOKEN, auth_mode: str = "static_app_token", base_url: str = _BASE) -> None:
    _write(
        {
            "easyauth": {
                "configured": True,
                "base_url": base_url,
                "app_key": _APP_KEY,
                "auth_mode": auth_mode,
                "credential": encrypt_secret(token),
                "has_credential": True,
            }
        }
    )


def _accepted() -> httpx.Response:
    return httpx.Response(
        200,
        json={"app_key": _APP_KEY, "already_up_to_date": False, "template_version": 3, "catalog_version": 4},
    )


def test_store_roundtrip_overwrites_single_row() -> None:
    _write({SETTING_KEY: None})
    store = PlatformSettingsManifestStore()
    assert store.load_state() is None
    store.save_state({"content_hash": "abc", "status": "ok", "schema_version": 2})
    assert store.load_state() == {"content_hash": "abc", "status": "ok", "schema_version": 2}
    store.save_state({"content_hash": "def", "status": "failed", "schema_version": 3})
    assert store.load_state() == {"content_hash": "def", "status": "failed", "schema_version": 3}
    with SessionLocal() as db:
        rows = db.query(PlatformSetting).filter(PlatformSetting.key == SETTING_KEY).all()
    assert len(rows) == 1


def test_target_uses_easyauth_row_and_oidc_frontend() -> None:
    _write(
        {
            "easyauth": {
                "base_url": "https://auth.example.test/",
                "app_key": " enterprise-blank ",
                "auth_mode": "static_app_token",
                "credential": encrypt_secret(_TOKEN),
            },
            "oidc": {"frontend_base_url": " https://blank.example.test/ "},
        }
    )
    target = load_manifest_sync_target()
    assert target == ManifestSyncTarget(
        "https://auth.example.test",
        "enterprise-blank",
        _TOKEN,
        "https://blank.example.test",
    )


def test_target_keeps_non_https_frontend() -> None:
    _put_easyauth()
    _write({"oidc": {"frontend_base_url": "http://blank.example.test"}})
    target = load_manifest_sync_target()
    assert target is not None
    assert target.public_base_url == "http://blank.example.test"


def test_target_missing_when_unconfigured_or_not_static() -> None:
    _write({"easyauth": None})
    assert load_manifest_sync_target() is None
    _write(
        {
            "easyauth": {
                "base_url": _BASE,
                "app_key": _APP_KEY,
                "auth_mode": "static_app_token",
                "credential": "not-encrypted",
            }
        }
    )
    assert load_manifest_sync_target() is None
    _put_easyauth(auth_mode="oauth_client_credentials")
    assert load_manifest_sync_target() is None


def test_blank_sync_posts_current_manifest_then_skips() -> None:
    _write({SETTING_KEY: None})
    _put_easyauth()
    _write({"oidc": {"frontend_base_url": "https://blank.example.test"}})
    bodies: list[dict[str, Any]] = []
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        paths.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        return _accepted()

    result = sync_blank_manifest(transport=httpx.MockTransport(handler))
    assert result.pushed is True
    assert paths == [f"/api/v1/apps/{_APP_KEY}/manifest-sync"]
    assert bodies[0]["base_url"] == "https://blank.example.test"
    assert bodies[0]["manifest"]["app"]["app_key"] == _APP_KEY
    assert bodies[0]["manifest"]["schema_version"] == 1
    assert bodies[0]["manifest"]["permissions"]
    stored = PlatformSettingsManifestStore().load_state()
    assert stored is not None
    assert stored["status"] == "ok"
    assert stored["base_url"] == _BASE
    assert stored["app_key"] == _APP_KEY
    assert stored["schema_version"] == 1
    second = sync_blank_manifest(transport=httpx.MockTransport(handler))
    assert second.skipped is True
    assert len(bodies) == 1


def test_unconfigured_sync_skips_without_http() -> None:
    _write({"easyauth": None, SETTING_KEY: None})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return _accepted()

    result = sync_blank_manifest(transport=httpx.MockTransport(handler))
    assert result.skipped is True
    assert result.pushed is False
    assert calls == []


def test_schedule_skipped_in_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setenv("BLANK_RUNTIME_ENV", "test")
    monkeypatch.setattr(
        "blank_app.easyauth_manifest_sync.sync_blank_manifest",
        lambda **kwargs: called.append(bool(kwargs.get("force"))),
    )
    schedule_blank_manifest_sync(force=True)
    assert called == []


def test_schedule_runs_outside_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    done = threading.Event()

    def fake(**kwargs: object) -> None:
        called.append(bool(kwargs.get("force")))
        done.set()

    monkeypatch.setenv("BLANK_RUNTIME_ENV", "production")
    monkeypatch.setattr("blank_app.easyauth_manifest_sync.sync_blank_manifest", fake)
    schedule_blank_manifest_sync(force=True)
    assert done.wait(timeout=2)
    assert called == [True]


def test_save_easyauth_settings_forces_manifest_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from blank_app.adapters import BlankIntegrationAdapter
    from enterprise_platform.schemas import EasyAuthSettingsUpdate

    calls: list[bool] = []
    monkeypatch.setattr(
        "blank_app.easyauth_manifest_sync.schedule_blank_manifest_sync",
        lambda *, force=False: calls.append(force),
    )
    BlankIntegrationAdapter().save_easyauth_settings(
        EasyAuthSettingsUpdate.model_validate({"baseUrl": _BASE, "appKey": _APP_KEY, "credential": _TOKEN}),
        actor_id="manifest-sync-test",
    )
    assert calls == [True]


async def test_lifespan_schedules_manifest_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from blank_app.main import app, lifespan

    calls: list[bool] = []
    monkeypatch.setattr("blank_app.main.schedule_blank_manifest_sync", lambda force=False: calls.append(force))
    async with lifespan(app):
        pass
    assert calls == [False]
