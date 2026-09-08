"""目录最低版本、失败退避、单飞与可信 principal 懒刷新。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from blank_app.database import SessionLocal
from blank_app.models import PermissionSnapshot
from enterprise_platform.authz.principal import UpstreamPrincipal
from platform_tests.test_easyauth_snapshot_pull import _account, _FakeClient, _store_snapshot

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def test_trusted_principal_does_not_pull_when_snapshot_fresh(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="principal-fresh")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    principal = UpstreamPrincipal(
        sub=account.external_user_id,
        issuer="https://identity.example.com/",
        audience="enterprise-blank",
        active=True,
        name=account.username,
        email="principal-fresh@example.com",
        external_source="authentik",
    )
    monkeypatch.setattr(authz_api, "parse_upstream_principal_from_headers", lambda *_args, **_kwargs: principal)
    assert authz_api.resolve_trusted_principal({}) == str(account.id)
    assert fake.calls == []


def test_commit_below_catalog_floor_is_rejected() -> None:
    from blank_app.authz_api import _commit_snapshot_row
    from blank_app.authz_snapshot import invalidate_app_snapshots

    account = _account(suffix="floor-stale")
    future = datetime.now(UTC) + timedelta(minutes=10)
    _store_snapshot(account, grant_version=5, catalog_version=3, expires_at=future)
    invalidate_app_snapshots("enterprise-blank", 4)
    with SessionLocal() as db:
        expired = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert expired.expires_at <= datetime.now(UTC)
        with pytest.raises(HTTPException) as captured:
            _commit_snapshot_row(
                db,
                PermissionSnapshot(
                    account_id=account.id,
                    external_source="authentik",
                    external_user_id=account.external_user_id,
                    app_key="enterprise-blank",
                ),
                external_source="authentik",
                external_user_id=account.external_user_id,
                app_key="enterprise-blank",
                values={
                    "account_id": account.id,
                    "groups": [],
                    "grants": [],
                    "grant_version": 5,
                    "catalog_version": 3,
                    "snapshot_version": "5.3-stale",
                    "fetched_at": datetime.now(UTC),
                    "expires_at": future,
                },
            )
        assert captured.value.status_code == 409
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.catalog_version == 3
        assert row.snapshot_version == "5.3"
        assert row.expires_at <= datetime.now(UTC)


def test_catalog_changed_replay_does_not_reexpire_refreshed_row(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import invalidate_app_snapshots, refresh_account_snapshot

    fake = _FakeClient()
    fake.grant_version = 5
    fake.catalog_version = 4
    fake.snapshot_version = "5.4"
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="floor-replay")
    _store_snapshot(account, grant_version=5, catalog_version=3, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    invalidate_app_snapshots("enterprise-blank", 4)
    refresh_account_snapshot(account.id)
    with SessionLocal() as db:
        refreshed = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert refreshed.catalog_version == 4
        assert refreshed.expires_at > datetime.now(UTC)
        kept_expiry = refreshed.expires_at
    invalidate_app_snapshots("enterprise-blank", 4)
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.catalog_version == 4
        assert row.expires_at == kept_expiry


def test_lazy_refresh_failure_uses_backoff(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import snapshot_grants_for_account

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="lazy-backoff")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert snapshot_grants_for_account(account) == ()
    assert fake.calls == [account.external_user_id]
    assert snapshot_grants_for_account(account) == ()
    assert fake.calls == [account.external_user_id]
    fake.fail = False
    assert authz_api.ensure_account_snapshot(account.id, force=True) is True
    assert fake.calls == [account.external_user_id, account.external_user_id]


def test_lazy_refresh_single_flight(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import snapshot_grants_for_account

    fake = _FakeClient()
    entered = threading.Event()
    release = threading.Event()
    original_fetch = fake.fetch_permission_snapshot
    entries: list[str] = []

    def blocked_fetch(user_id: str):
        entries.append(user_id)
        entered.set()
        assert release.wait(timeout=5)
        return original_fetch(user_id)

    fake.fetch_permission_snapshot = blocked_fetch  # type: ignore[method-assign]
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="single-flight")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    results: list[object] = [None, None]

    def run(index: int) -> None:
        results[index] = snapshot_grants_for_account(account)

    first = threading.Thread(target=run, args=(0,))
    second = threading.Thread(target=run, args=(1,))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    deadline = time.time() + 0.3
    while time.time() < deadline and len(entries) < 2:
        time.sleep(0.01)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert entries == [account.external_user_id]
    assert fake.calls == [account.external_user_id]
    assert {grant.code for grants in results for grant in grants} == {"authz.integration.view"}
