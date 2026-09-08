"""登录强制拉取、过期懒刷新与 (grant_version, catalog_version) 守卫。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blank_app.database import SessionLocal
from blank_app.models import Account, PermissionSnapshot, PlatformSetting
from enterprise_platform.authz import EasyAuthClientError, EasyAuthPermissionSnapshot
from enterprise_platform.schemas import EasyAuthSettingsUpdate

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


class _FakeClient:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[str] = []
        self.grant_version = 2
        self.catalog_version = 3
        self.snapshot_version = "2.3"

    def fetch_permission_snapshot(self, user_id: str) -> EasyAuthPermissionSnapshot:
        self.calls.append(user_id)
        if self.fail:
            raise EasyAuthClientError("EasyAuth permission query failed")
        return EasyAuthPermissionSnapshot.model_validate(
            {
                "user_id": user_id,
                "app_key": "enterprise-blank",
                "groups": [],
                "grants": [
                    {
                        "permission": "authz.integration.view",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    }
                ],
                "grant_version": self.grant_version,
                "catalog_version": self.catalog_version,
                "snapshot_version": self.snapshot_version,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            }
        )


def _account(*, suffix: str) -> Account:
    with SessionLocal() as db:
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {**(setting.value or {}), "app_key": "enterprise-blank"}
        account = Account(
            username=f"pull-{suffix}",
            email=None,
            password_hash=None,
            external_source="authentik",
            external_user_id=f"pull-{suffix}",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add_all([setting, account])
        db.commit()
        db.refresh(account)
        db.expunge(account)
        return account


def _store_snapshot(account: Account, *, grant_version: int, catalog_version: int, expires_at: datetime) -> None:
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
                grant_version=grant_version,
                catalog_version=catalog_version,
                snapshot_version=f"{grant_version}.{catalog_version}",
                expires_at=expires_at,
            )
        )
        db.commit()


def test_login_force_refresh_pulls_unexpired_cache(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="force-fresh")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    assert authz_api.ensure_account_snapshot(account.id, force=False) is True
    assert fake.calls == []
    assert authz_api.ensure_account_snapshot(account.id, force=True) is True
    assert fake.calls == [account.external_user_id]


def test_login_force_refresh_keeps_last_good_row_on_failure(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="force-keep")
    _store_snapshot(account, grant_version=2, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    assert authz_api.ensure_account_snapshot(account.id, force=True) is True
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.grant_version == 2
        assert row.snapshot_version == "2.1"


def test_login_force_refresh_without_row_fails_closed(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="force-empty")
    assert authz_api.ensure_account_snapshot(account.id, force=True) is False
    with SessionLocal() as db:
        assert db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).count() == 0


def test_permission_check_lazily_refreshes_expired_snapshot(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import snapshot_grants_for_account

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="lazy-ok")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    grants = snapshot_grants_for_account(account)
    assert fake.calls == [account.external_user_id]
    assert {grant.code for grant in grants} == {"authz.integration.view"}


def test_permission_check_refresh_failure_is_zero(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import snapshot_grants_for_account

    fake = _FakeClient()
    fake.fail = True
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="lazy-zero")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert snapshot_grants_for_account(account) == ()


def test_catalog_only_version_overwrites_same_grant_version() -> None:
    from blank_app.authz_api import _commit_snapshot_row

    account = _account(suffix="catalog-guard")
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    with SessionLocal() as db:
        row = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key="enterprise-blank",
        )
        _commit_snapshot_row(
            db,
            row,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key="enterprise-blank",
            values={
                "account_id": account.id,
                "groups": [],
                "grants": [],
                "grant_version": 5,
                "catalog_version": 3,
                "snapshot_version": "5.3",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
        persisted = _commit_snapshot_row(
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
                "grants": [
                    {
                        "permission": "authz.integration.view",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    }
                ],
                "grant_version": 5,
                "catalog_version": 4,
                "snapshot_version": "5.4",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
        assert persisted.catalog_version == 4
        assert persisted.snapshot_version == "5.4"


def test_older_catalog_version_cannot_overwrite() -> None:
    from blank_app.authz_api import _commit_snapshot_row

    account = _account(suffix="catalog-stale")
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    with SessionLocal() as db:
        row = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key="enterprise-blank",
        )
        _commit_snapshot_row(
            db,
            row,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key="enterprise-blank",
            values={
                "account_id": account.id,
                "groups": [],
                "grants": [],
                "grant_version": 5,
                "catalog_version": 4,
                "snapshot_version": "5.4",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
        persisted = _commit_snapshot_row(
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
                "expires_at": expires_at,
            },
        )
        assert persisted.catalog_version == 4
        assert persisted.snapshot_version == "5.4"


def test_grant_changed_is_idempotent_when_version_already_stored(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.authz_snapshot import refresh_snapshot_for_external_user

    fake = _FakeClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    account = _account(suffix="idempotent")
    _store_snapshot(account, grant_version=4, catalog_version=2, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    refresh_snapshot_for_external_user(account.external_user_id, "4.2")
    assert fake.calls == []


def test_invalidate_app_snapshots_marks_rows_expired() -> None:
    from blank_app.authz_snapshot import invalidate_app_snapshots

    account = _account(suffix="invalidate")
    _store_snapshot(account, grant_version=1, catalog_version=1, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    invalidate_app_snapshots("enterprise-blank")
    with SessionLocal() as db:
        row = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).one()
        assert row.expires_at <= datetime.now(UTC)


def test_webhook_secret_is_encrypted_like_credential() -> None:
    from blank_app.adapters import BlankIntegrationAdapter
    from blank_app.models import PlatformAuditLog
    from enterprise_platform.secrets import decrypt_secret

    adapter = BlankIntegrationAdapter()
    status = adapter.save_easyauth_settings(
        EasyAuthSettingsUpdate.model_validate(
            {
                "baseUrl": "https://authz-events.example.com",
                "appKey": "enterprise-blank",
                "credential": "app-token",
                "webhookSecret": "whsec_plain",
            }
        ),
        actor_id="test-actor",
    )
    assert status.has_webhook_secret is True
    assert adapter.get_easyauth_webhook_secret() == "whsec_plain"
    with SessionLocal() as db:
        stored = db.get(PlatformSetting, "easyauth").value["webhook_secret"]
        assert stored.startswith("enc:v1:")
        assert decrypt_secret(stored) == "whsec_plain"
        audit = (
            db.query(PlatformAuditLog)
            .filter(PlatformAuditLog.action == "authz.settings.update")
            .order_by(PlatformAuditLog.created_at.desc())
            .first()
        )
        assert audit.after_data["webhook_secret"] == "[configured]"
