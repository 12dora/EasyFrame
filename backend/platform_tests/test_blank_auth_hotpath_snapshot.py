"""认证热路径:过期 fail-closed、近过期后台刷新、STALE_GRACE 与显式失效。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from blank_app.adapters import account_adapter
from blank_app.authz_hotpath import wait_background_refreshes
from blank_app.authz_snapshot import snapshot_grants_for_account
from blank_app.database import SessionLocal
from blank_app.models import PermissionSnapshot
from enterprise_platform.authz.snapshot_freshness import STALE_GRACE
from platform_tests.test_easyauth_snapshot_pull import _account, _FakeClient, _store_snapshot

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def test_expired_snapshot_is_fail_closed_and_not_served_from_background(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.main import app

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    scheduled: list[str] = []
    monkeypatch.setattr(
        "blank_app.authz_hotpath.schedule_background_refresh",
        lambda account_id, pull_key: scheduled.append(str(pull_key)),
    )
    account = _account(suffix="expired-zero")
    _store_beyond_grace(account)
    grants = snapshot_grants_for_account(account)
    wait_background_refreshes()
    assert grants == ()
    assert scheduled == []
    headers = {"Authorization": f"Bearer {account_adapter.issue_session(str(account.id))}"}
    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["permissions"] == []
    assert me.json()["grants"] == []


def test_near_expiry_snapshot_schedules_exactly_one_background_refresh(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    entered = threading.Event()
    release = threading.Event()
    original = fake.fetch_permission_snapshot
    starts = {"n": 0}

    def blocked(user_id: str):
        starts["n"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(user_id)

    fake.fetch_permission_snapshot = blocked  # type: ignore[method-assign]
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="near-expiry")
    _store_near_expiry(account)
    results: list[set[str]] = []

    def run() -> None:
        results.append({grant.code for grant in snapshot_grants_for_account(account)})

    started = [threading.Thread(target=run) for _ in range(2)]
    for thread in started:
        thread.start()
    for thread in started:
        thread.join(timeout=5)
    assert entered.wait(timeout=5)
    assert starts["n"] == 1
    assert results == [{"authz.integration.view"}, {"authz.integration.view"}]
    release.set()
    wait_background_refreshes()
    assert starts["n"] == 1
    assert fake.calls == [account.external_user_id]


def test_fresh_snapshot_does_not_schedule_background(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    scheduled: list[str] = []
    monkeypatch.setattr(
        "blank_app.authz_hotpath.schedule_background_refresh",
        lambda account_id, pull_key: scheduled.append(str(pull_key)),
    )
    account = _account(suffix="fresh-no-bg")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    grants = snapshot_grants_for_account(account)
    assert {grant.code for grant in grants} == {"authz.integration.view"}
    assert scheduled == []
    assert fake.calls == []


def test_stale_grace_serves_grants_and_refreshes_in_background(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="stale-grace")
    _store_stale_grace(account)
    grants = snapshot_grants_for_account(account)
    assert {grant.code for grant in grants} == {"authz.integration.view"}
    assert fake.calls == []
    wait_background_refreshes()
    assert fake.calls == [account.external_user_id]


def test_beyond_grace_sync_pulls(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="beyond-grace")
    _store_beyond_grace(account)
    grants = snapshot_grants_for_account(account)
    assert fake.calls == [account.external_user_id]
    assert {grant.code for grant in grants} == {"authz.integration.view"}


def test_invalidated_snapshot_is_never_served(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="invalidated")
    now = datetime.now(UTC)
    _store_timed_snapshot(account, fetched_at=now - timedelta(minutes=1), expires_at=now - timedelta(minutes=1))
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.expires_at == row.fetched_at
    assert snapshot_grants_for_account(account) == ()


def test_invalidate_app_snapshots_sets_expires_at_to_fetched_at() -> None:
    from blank_app.authz_snapshot import invalidate_app_snapshots

    account = _account(suffix="invalidate-eq")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    invalidate_app_snapshots("enterprise-blank", 2)
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.expires_at == row.fetched_at


def test_unexpired_below_floor_snapshot_still_serves_role_groups(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_catalog_floor import raise_catalog_floor, reset_catalog_floor
    from blank_app.main import app

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="groups-below-floor")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        row.groups = [{"name": "教研组"}]
        db.commit()
    with SessionLocal() as db:
        raise_catalog_floor(db, "enterprise-blank", 9)
        db.commit()
    try:
        assert snapshot_grants_for_account(account) == ()
        headers = {"Authorization": f"Bearer {account_adapter.issue_session(str(account.id))}"}
        with TestClient(app) as client:
            me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["permissions"] == []
        assert body["grants"] == []
        assert body["roleGroups"] == ["教研组"]
    finally:
        reset_catalog_floor()


def _store_near_expiry(account) -> None:
    now = datetime.now(UTC)
    _store_timed_snapshot(account, fetched_at=now - timedelta(seconds=80), expires_at=now + timedelta(seconds=20))


def _store_stale_grace(account) -> None:
    now = datetime.now(UTC)
    _store_timed_snapshot(account, fetched_at=now - timedelta(minutes=11), expires_at=now - timedelta(minutes=1))


def _store_beyond_grace(account) -> None:
    now = datetime.now(UTC)
    _store_timed_snapshot(
        account,
        fetched_at=now - STALE_GRACE - timedelta(minutes=10),
        expires_at=now - STALE_GRACE - timedelta(seconds=1),
    )


def _store_timed_snapshot(account, *, fetched_at: datetime, expires_at: datetime) -> None:
    with SessionLocal() as db:
        db.add(
            PermissionSnapshot(
                account_id=account.id,
                external_source=account.external_source,
                external_user_id=account.external_user_id,
                app_key="enterprise-blank",
                groups=[],
                grants=[
                    {
                        "permission": "authz.integration.view",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    }
                ],
                grant_version=1,
                catalog_version=1,
                snapshot_version="1.1",
                fetched_at=fetched_at,
                expires_at=expires_at,
            )
        )
        db.commit()
