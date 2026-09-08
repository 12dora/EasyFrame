"""目录下限乱序事件、写入竞态、强制拉取与 authority 切换。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from blank_app.authz_catalog_floor import catalog_floor, raise_catalog_floor
from blank_app.database import SessionLocal
from blank_app.models import Account, PermissionSnapshot, PlatformSetting
from platform_tests.test_easyauth_snapshot_pull import _account, _FakeClient, _store_snapshot

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")
APP_KEY = "enterprise-blank"


def _write_snapshot(account: Account, *, catalog_version: int, grant_version: int = 1, db=None) -> int:
    from blank_app.authz_api import _commit_snapshot_row

    values = {
        "account_id": account.id,
        "groups": [],
        "grants": [],
        "grant_version": grant_version,
        "catalog_version": catalog_version,
        "snapshot_version": f"{grant_version}.{catalog_version}",
        "fetched_at": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    row = PermissionSnapshot(
        account_id=account.id,
        external_source="authentik",
        external_user_id=account.external_user_id,
        app_key=APP_KEY,
    )

    def persist(session) -> int:
        written = _commit_snapshot_row(
            session,
            row,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key=APP_KEY,
            values=values,
        )
        return written.catalog_version

    if db is not None:
        return persist(db)
    with SessionLocal() as session:
        return persist(session)


def _set_base_url(url: str) -> None:
    with SessionLocal() as db:
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {**(setting.value or {}), "app_key": APP_KEY, "base_url": url.rstrip("/")}
        db.add(setting)
        db.commit()


def _block_first_fetch(fake: _FakeClient, *, stale_version: str = "1.1-stale"):
    from enterprise_platform.authz import EasyAuthPermissionSnapshot

    entered = threading.Event()
    release = threading.Event()
    original = fake.fetch_permission_snapshot
    first = True
    stale = EasyAuthPermissionSnapshot.model_validate(
        {
            "user_id": "stale",
            "app_key": APP_KEY,
            "groups": [],
            "grants": [
                {
                    "permission": "authz.integration.view",
                    "scope": "ALL",
                    "source_type": "direct",
                    "source_key": "",
                }
            ],
            "grant_version": 1,
            "catalog_version": 1,
            "snapshot_version": stale_version,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        }
    )

    def blocked_fetch(user_id: str):
        nonlocal first
        if first:
            first = False
            fake.calls.append(user_id)
            entered.set()
            assert release.wait(timeout=5)
            return stale
        return original(user_id)

    fake.fetch_permission_snapshot = blocked_fetch  # type: ignore[method-assign]
    return entered, release


def test_out_of_order_catalog_events_keep_highest_floor() -> None:
    from blank_app.authz_snapshot import invalidate_app_snapshots

    account = _account(suffix="ooo-floor")
    invalidate_app_snapshots(APP_KEY, 10)
    invalidate_app_snapshots(APP_KEY, 8)
    with SessionLocal() as db:
        assert catalog_floor(db, APP_KEY) == 10
    with pytest.raises(HTTPException) as captured:
        _write_snapshot(account, catalog_version=9)
    assert captured.value.status_code == 409
    assert _write_snapshot(account, catalog_version=10) == 10


def test_concurrent_floor_first_inserts_use_greatest() -> None:
    from blank_app.authz_snapshot import invalidate_app_snapshots

    _account(suffix="floor-greatest")
    errors: list[BaseException] = []

    def raise_to(version: int) -> None:
        try:
            invalidate_app_snapshots(APP_KEY, version)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=raise_to, args=(version,)) for version in (5, 10, 7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    with SessionLocal() as db:
        assert catalog_floor(db, APP_KEY) == 10


def test_floor_vs_write_race_rejects_stale_catalog() -> None:
    account = _account(suffix="floor-write-race")
    future = datetime.now(UTC) + timedelta(minutes=10)
    _store_snapshot(account, grant_version=5, catalog_version=3, expires_at=future)
    locked = threading.Event()
    release = threading.Event()
    outcomes: list[object] = []

    def holder() -> None:
        with SessionLocal() as db:
            raise_catalog_floor(db, APP_KEY, 4)
            locked.set()
            release.wait(timeout=5)
            db.commit()

    def writer() -> None:
        locked.wait(timeout=5)
        try:
            with SessionLocal() as db:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                _write_snapshot(account, catalog_version=3, grant_version=5, db=db)
            outcomes.append("wrote")
        except HTTPException as exc:
            outcomes.append(exc.status_code)
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=holder), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    assert locked.wait(timeout=5)
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=8)
    assert outcomes == [409]


def test_forced_pull_after_stale_flight_does_not_keep_pre_event_result(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import refresh_snapshot_for_external_user, snapshot_grants_for_account

    fake = _FakeClient()
    fake.grant_version = 1
    fake.catalog_version = 1
    fake.snapshot_version = "1.1"
    entered, release = _block_first_fetch(fake)
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="stale-flight")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    force_done = threading.Event()
    lazy = threading.Thread(target=lambda: snapshot_grants_for_account(account))
    lazy.start()
    assert entered.wait(timeout=5)
    fake.grant_version = 2
    fake.snapshot_version = "2.1"

    def run_force() -> None:
        refresh_snapshot_for_external_user(account.external_user_id, "2.1")
        force_done.set()

    force = threading.Thread(target=run_force)
    force.start()
    deadline = time.time() + 0.3
    while time.time() < deadline and not force_done.is_set():
        time.sleep(0.01)
    assert not force_done.is_set()
    release.set()
    force.join(timeout=5)
    lazy.join(timeout=5)
    assert force_done.is_set()
    assert fake.calls.count(account.external_user_id) >= 2
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.snapshot_version == "2.1"


def test_login_force_refresh_waits_for_stale_flight(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    fake.grant_version = 1
    fake.catalog_version = 1
    fake.snapshot_version = "1.1"
    entered, release = _block_first_fetch(fake)
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="login-stale-flight")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    force_done = threading.Event()
    lazy = threading.Thread(target=lambda: authz_api.ensure_account_snapshot(account.id, force=False))
    lazy.start()
    assert entered.wait(timeout=5)
    fake.grant_version = 3
    fake.snapshot_version = "3.1"

    def run_force() -> None:
        assert authz_api.ensure_account_snapshot(account.id, force=True) is True
        force_done.set()

    force = threading.Thread(target=run_force)
    force.start()
    deadline = time.time() + 0.3
    while time.time() < deadline and not force_done.is_set():
        time.sleep(0.01)
    assert not force_done.is_set()
    release.set()
    force.join(timeout=5)
    lazy.join(timeout=5)
    assert force_done.is_set()
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.snapshot_version == "3.1"


def test_authority_change_does_not_apply_old_floor() -> None:
    from blank_app.adapters import BlankIntegrationAdapter
    from blank_app.authz_snapshot import invalidate_app_snapshots
    from enterprise_platform.schemas import EasyAuthSettingsUpdate

    account = _account(suffix="authority-floor")
    _set_base_url("https://easyauth-a.example")
    invalidate_app_snapshots(APP_KEY, 10)
    with SessionLocal() as db:
        assert catalog_floor(db, APP_KEY) == 10
    _set_base_url("https://easyauth-b.example")
    with SessionLocal() as db:
        assert catalog_floor(db, APP_KEY) == 0
    assert _write_snapshot(account, catalog_version=1) == 1
    invalidate_app_snapshots(APP_KEY, 9)
    BlankIntegrationAdapter().save_easyauth_settings(
        EasyAuthSettingsUpdate.model_validate(
            {"baseUrl": "https://easyauth-c.example", "appKey": APP_KEY, "credential": "token"}
        ),
        actor_id="test-actor",
    )
    with SessionLocal() as db:
        assert catalog_floor(db, APP_KEY) == 0
    assert _write_snapshot(account, catalog_version=1) == 1


def test_cache_read_rejects_row_below_catalog_floor(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import snapshot_grants_for_account

    fake = _FakeClient()
    fake.grant_version = 5
    fake.catalog_version = 4
    fake.snapshot_version = "5.4"
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="floor-cache")
    _store_snapshot(account, grant_version=5, catalog_version=3, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    with SessionLocal() as db:
        raise_catalog_floor(db, APP_KEY, 4)
        db.commit()
    grants = snapshot_grants_for_account(account)
    assert fake.calls == [account.external_user_id]
    assert {grant.code for grant in grants} == {"authz.integration.view"}
