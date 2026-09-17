"""认证热路径:近静态缓存失效与 /auth/me SQL 上界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from blank_app.authz_cache import cached_catalog_floor, catalog_map
from blank_app.authz_snapshot import invalidate_app_snapshots, seed_platform_catalog
from blank_app.database import SessionLocal, engine
from blank_app.models import PermissionCatalog

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

_BUSINESS_TABLES = (
    "platform_accounts",
    "platform_passkeys",
    "platform_permission_snapshots",
    "platform_permission_catalog",
    "platform_settings",
)


@contextmanager
def count_sql() -> Iterator[list[str]]:
    statements: list[str] = []

    def before(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _business_sql(statements: list[str]) -> list[str]:
    lowered = [item.lower() for item in statements]
    return [item for item in lowered if any(table in item for table in _BUSINESS_TABLES)]


def test_seed_platform_catalog_invalidates_catalog_cache() -> None:
    from blank_app.authz_cache import invalidate_catalog

    extra = "zz.hotpath.cache"
    with SessionLocal() as db:
        if db.get(PermissionCatalog, extra) is None:
            db.add(
                PermissionCatalog(
                    code=extra,
                    name_zh="缓存探针",
                    name_en="cache probe",
                    domain="test",
                    resource="cache",
                    supported_scopes=["SELF"],
                    risk_level="standard",
                    active=True,
                )
            )
            db.commit()
    try:
        catalog_map()
        with SessionLocal() as db:
            db.query(PermissionCatalog).filter(PermissionCatalog.code == extra).update(
                {PermissionCatalog.active: False},
                synchronize_session=False,
            )
            db.commit()
        assert catalog_map()[extra].active is True
        seed_platform_catalog()
        assert catalog_map()[extra].active is False
    finally:
        with SessionLocal() as db:
            row = db.get(PermissionCatalog, extra)
            if row is not None:
                db.delete(row)
                db.commit()
        invalidate_catalog()


def test_invalidate_app_snapshots_invalidates_floor_cache() -> None:
    from blank_app.authz_catalog_floor import reset_catalog_floor

    app_key = "enterprise-blank"
    reset_catalog_floor()
    with SessionLocal() as db:
        assert cached_catalog_floor(db, app_key) == 0
    invalidate_app_snapshots(app_key, 11)
    with SessionLocal() as db:
        assert cached_catalog_floor(db, app_key) == 11
    reset_catalog_floor()


def test_auth_me_sql_bound_with_fresh_snapshot() -> None:
    from datetime import UTC, datetime, timedelta

    from blank_app.adapters import account_adapter
    from blank_app.authz_cache import cached_app_key, cached_easyauth_setting
    from blank_app.main import app
    from platform_tests.test_easyauth_snapshot_pull import _account, _store_snapshot

    catalog_map()
    cached_easyauth_setting()
    with SessionLocal() as db:
        cached_catalog_floor(db, cached_app_key(db) or "enterprise-blank")
    account = _account(suffix="sql-bound")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    headers = {"Authorization": f"Bearer {account_adapter.issue_session(str(account.id))}"}
    with TestClient(app) as client:
        client.get("/api/v1/auth/me", headers=headers)
        with count_sql() as statements:
            response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    business = _business_sql(statements)
    assert len(business) <= 6, business


def test_save_easyauth_settings_updates_cached_app_key_immediately() -> None:
    from blank_app.adapter_platform import BlankIntegrationAdapter
    from blank_app.adapter_support import _get_setting, _save_setting
    from blank_app.authz_cache import cached_app_key, invalidate_easyauth
    from enterprise_platform.schemas import EasyAuthSettingsUpdate

    previous = _get_setting("easyauth")
    cached_app_key()
    try:
        BlankIntegrationAdapter().save_easyauth_settings(
            EasyAuthSettingsUpdate.model_validate(
                {
                    "baseUrl": str(previous.get("base_url") or "https://easyauth.example.test"),
                    "appKey": "hotpath-cache-key",
                    "credential": "token",
                }
            ),
            actor_id="test-actor",
        )
        assert cached_app_key() == "hotpath-cache-key"
    finally:
        _save_setting("easyauth", previous, actor_id="test-actor", action="authz.settings.update")
        invalidate_easyauth()


def test_raise_catalog_floor_invalidates_only_after_commit() -> None:
    from blank_app.authz_cache import cached_catalog_floor
    from blank_app.authz_catalog_floor import raise_catalog_floor, reset_catalog_floor

    app_key = "enterprise-blank"
    reset_catalog_floor()
    with SessionLocal() as db:
        assert cached_catalog_floor(db, app_key) == 0
    with SessionLocal() as writer:
        raise_catalog_floor(writer, app_key, 7)
        with SessionLocal() as reader:
            assert cached_catalog_floor(reader, app_key) == 0
        writer.commit()
    with SessionLocal() as reader:
        assert cached_catalog_floor(reader, app_key) == 7
    reset_catalog_floor()


def test_ttl_box_late_set_does_not_clobber_newer_invalidate() -> None:
    from enterprise_platform.ttl_cache import TtlBox

    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    box.set("old", generation=box.generation)
    generation = box.generation
    box.invalidate()
    box.set("stale", generation=generation)
    hit, value = box.peek()
    assert hit is False and value is None
    box.set("fresh", generation=box.generation)
    assert box.peek() == (True, "fresh")


def test_setting_value_copies_json() -> None:
    from blank_app.authz_cache import _setting_value
    from blank_app.models import PlatformSetting

    with SessionLocal() as db:
        copied = _setting_value(db, "easyauth")
        row = db.get(PlatformSetting, "easyauth")
        assert copied is not None and row is not None and row.value is not None
        assert copied is not row.value
        copied["app_key"] = "mutated-copy"
        assert row.value.get("app_key") != "mutated-copy"


def test_catalog_map_single_flight_on_miss(monkeypatch) -> None:
    import threading
    import time

    from blank_app import authz_cache as cache

    cache.invalidate_catalog()
    calls = {"n": 0}
    original = cache._load_catalog
    entered = threading.Event()
    release = threading.Event()

    def slow(db=None):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(db)

    monkeypatch.setattr(cache, "_load_catalog", slow)
    results: list[object] = []

    def run() -> None:
        results.append(cache.catalog_map())

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    assert calls["n"] == 1
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert calls["n"] == 1
    assert len(results) == 2


def test_catalog_map_reuses_caller_db_session(monkeypatch) -> None:
    from blank_app import authz_cache as cache

    cache.invalidate_catalog()

    def boom():
        raise AssertionError("must reuse caller session")

    monkeypatch.setattr(cache, "SessionLocal", boom)
    with SessionLocal() as db:
        mapping = cache.catalog_map(db)
    assert mapping
