"""blank_app 对共享 local-accounts 宿主一致性套件的实现。"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator, Mapping, Sequence

import pyotp
import pytest
from fastapi.testclient import TestClient

from blank_app.database import SessionLocal
from blank_app.models import (
    Account,
    Notification,
    Passkey,
    PermissionCatalog,
    PermissionSnapshot,
    PlatformAuditLog,
)
from platform_tests.local_accounts_conformance import (
    AuditRecord,
    HOST_CONTRACT_MEMBERS,
    LocalGrant,
    LoginModes,
    RawAccount,
    check_auth_me_identity,
    check_crud_local_grants_cas_and_audit,
    check_expiry_catalog_and_sso_visibility,
    check_host_contract_member,
    check_live_login_eligibility,
    check_operator_target_guards,
    check_router_mount_and_permission_gates,
)

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


class BlankLocalAccountsConformanceHost:
    api_prefix = "/api/v1"
    auth_me_path = "/auth/me"
    login_path = "/auth/login"
    login_modes = LoginModes()

    def __init__(self) -> None:
        self.created_account_ids: list[str] = []

    @property
    def app(self):
        from blank_app.main import app

        return app

    @property
    def admin_username(self) -> str:
        return os.environ["BLANK_ADMIN_USERNAME"]

    @property
    def admin_password(self) -> str:
        return os.environ["BLANK_ADMIN_PASSWORD"]

    def ensure_admin(self) -> RawAccount:
        with SessionLocal() as db:
            account = db.query(Account).filter(Account.username == self.admin_username).one()
            account.active = True
            account.is_admin = True
            account.must_change_password = False
            account.expires_at = None
            account.totp_enabled = False
            account.totp_secret = None
            account.totp_pending_secret = None
            db.commit()
            return RawAccount(str(account.id), account.username, self.admin_password)

    def create_raw_account(
        self,
        *,
        username: str,
        password: str | None,
        is_admin: bool = False,
        grants: Sequence[LocalGrant] = (),
        expires_at: datetime | None = None,
        must_change_password: bool = False,
        external: bool = False,
    ) -> RawAccount:
        from blank_app.adapters import pwd_context

        account = Account(
            username=username,
            email=f"{username}@example.com",
            password_hash=None if external or password is None else pwd_context.hash(password),
            external_source="conformance-sso" if external else None,
            external_user_id=uuid.uuid4().hex if external else None,
            active=True,
            is_admin=is_admin,
            must_change_password=must_change_password,
            local_permissions=[dict(grant) for grant in grants],
            expires_at=expires_at,
        )
        with SessionLocal() as db:
            db.add(account)
            db.commit()
            db.refresh(account)
            result = RawAccount(str(account.id), account.username, password)
            self.created_account_ids.append(result.id)
            return result

    def issue_session(self, account_id: str) -> str:
        from blank_app.adapters import account_adapter

        return account_adapter.issue_session(account_id)

    def set_account_expiry(self, account_id: str, expires_at: datetime | None) -> None:
        with SessionLocal() as db:
            account = db.get(Account, uuid.UUID(account_id))
            assert account is not None
            account.expires_at = expires_at
            db.commit()

    def remove_raw_account(self, account_id: str) -> None:
        parsed_id = uuid.UUID(account_id)
        with SessionLocal() as db:
            db.query(Passkey).filter(Passkey.account_id == parsed_id).delete()
            db.query(Notification).filter(Notification.account_id == parsed_id).delete()
            db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == parsed_id).delete()
            for audit in db.query(PlatformAuditLog).all():
                targets = {
                    (audit.before_data or {}).get("targetAccountId"),
                    (audit.after_data or {}).get("targetAccountId"),
                }
                if audit.actor_id == account_id or account_id in targets:
                    db.delete(audit)
            account = db.get(Account, parsed_id)
            if account is not None:
                db.delete(account)
            db.commit()
        if account_id in self.created_account_ids:
            self.created_account_ids.remove(account_id)

    @contextmanager
    def only_usable_admin(self, account_id: str) -> Iterator[None]:
        target_id = uuid.UUID(account_id)
        with SessionLocal() as db:
            admins = db.query(Account).filter(Account.external_source.is_(None), Account.is_admin.is_(True)).all()
            state = [(row.id, row.active, row.is_admin, row.expires_at) for row in admins]
            target = db.get(Account, target_id)
            assert target is not None
            target.is_admin = True
            target.active = True
            target.expires_at = None
            for row in admins:
                if row.id != target_id:
                    row.active = False
            db.commit()
        try:
            yield
        finally:
            with SessionLocal() as db:
                for row_id, active, is_admin, expires_at in state:
                    row = db.get(Account, row_id)
                    if row is not None:
                        row.active = active
                        row.is_admin = is_admin
                        row.expires_at = expires_at
                db.commit()

    def audit_rows(self, account_id: str) -> list[AuditRecord]:
        with SessionLocal() as db:
            rows = db.query(PlatformAuditLog).order_by(PlatformAuditLog.created_at.asc()).all()
            return [
                AuditRecord(row.action, row.before_data, row.after_data)
                for row in rows
                if (row.after_data or {}).get("targetAccountId") == account_id
                or (row.before_data or {}).get("targetAccountId") == account_id
            ]

    def add_catalog_permission(
        self,
        *,
        code: str,
        supported_scopes: Sequence[str],
        risk_level: str = "standard",
    ) -> None:
        with SessionLocal() as db:
            db.add(
                PermissionCatalog(
                    code=code,
                    name_zh="一致性测试权限",
                    name_en="Conformance permission",
                    domain="conformance",
                    resource=code,
                    group_key=None,
                    supported_scopes=list(supported_scopes),
                    risk_level=risk_level,
                    active=True,
                )
            )
            db.commit()

    def remove_catalog_permission(self, code: str) -> None:
        with SessionLocal() as db:
            row = db.get(PermissionCatalog, code)
            if row is not None:
                db.delete(row)
                db.commit()

    def seed_account_dependents(self, account_id: str) -> None:
        parsed_id = uuid.UUID(account_id)
        suffix = uuid.uuid4().hex
        with SessionLocal() as db:
            assert db.get(Account, parsed_id) is not None
            db.add(Notification(account_id=parsed_id, title="conformance dependent"))
            db.add(
                Passkey(
                    account_id=parsed_id,
                    credential_id=f"conformance-{suffix}",
                    public_key="conformance-public-key",
                    sign_count=0,
                    name="conformance-device",
                )
            )
            db.add(
                PermissionSnapshot(
                    account_id=parsed_id,
                    external_source="conformance",
                    external_user_id=f"conformance-{suffix}",
                    app_key="conformance",
                    groups=[],
                    grants=[],
                    grant_version=1,
                    catalog_version=1,
                    snapshot_version="conformance-1",
                    fetched_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            db.commit()

    def count_account_dependents(self, account_id: str) -> Mapping[str, int]:
        parsed_id = uuid.UUID(account_id)
        with SessionLocal() as db:
            return {
                "passkeys": db.query(Passkey.id).filter(Passkey.account_id == parsed_id).count(),
                "notifications": db.query(Notification.id).filter(Notification.account_id == parsed_id).count(),
                "permissionSnapshots": db.query(PermissionSnapshot.id)
                .filter(PermissionSnapshot.account_id == parsed_id)
                .count(),
            }

    @staticmethod
    def extract_login_token(response_json: Mapping[str, object]) -> str:
        token = response_json.get("accessToken")
        assert isinstance(token, str)
        return token

    @staticmethod
    def extract_me(response_json: Mapping[str, object]) -> Mapping[str, object]:
        return response_json

    @contextmanager
    def local_auth_mode(self, mode: str) -> Iterator[None]:
        previous = os.environ.get("BLANK_LOCAL_AUTH_MODE")
        os.environ["BLANK_LOCAL_AUTH_MODE"] = mode
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("BLANK_LOCAL_AUTH_MODE", None)
            else:
                os.environ["BLANK_LOCAL_AUTH_MODE"] = previous


@pytest.fixture
def local_accounts_host() -> Iterator[BlankLocalAccountsConformanceHost]:
    host = BlankLocalAccountsConformanceHost()
    try:
        yield host
    finally:
        for account_id in reversed(host.created_account_ids.copy()):
            host.remove_raw_account(account_id)


@pytest.mark.parametrize("member", HOST_CONTRACT_MEMBERS)
def test_blank_host_implements_conformance_contract(
    local_accounts_host: BlankLocalAccountsConformanceHost, member: str
) -> None:
    check_host_contract_member(local_accounts_host, member)


def test_router_mount_and_permission_gates(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_router_mount_and_permission_gates(local_accounts_host)


def test_crud_local_grants_cas_and_audit(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_crud_local_grants_cas_and_audit(local_accounts_host)


def test_operator_target_guards(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_operator_target_guards(local_accounts_host)


def test_expiry_catalog_and_sso_visibility(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_expiry_catalog_and_sso_visibility(local_accounts_host)


def test_auth_me_identity(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_auth_me_identity(local_accounts_host)


def test_live_login_eligibility(local_accounts_host: BlankLocalAccountsConformanceHost) -> None:
    check_live_login_eligibility(local_accounts_host)


def test_totp_rescue_clears_blank_secrets_and_redacts_audit(
    local_accounts_host: BlankLocalAccountsConformanceHost,
) -> None:
    account = local_accounts_host.create_raw_account(
        username=f"totp-rescue-{uuid.uuid4().hex[:10]}",
        password="Conformance-totp-rescue-password-42!",
    )
    active_secret = pyotp.random_base32()
    pending_secret = "conformance-pending-secret"
    with SessionLocal() as db:
        row = db.get(Account, uuid.UUID(account.id))
        assert row is not None
        row.totp_enabled = True
        row.totp_secret = active_secret
        row.totp_pending_secret = pending_secret
        db.commit()

    admin = local_accounts_host.ensure_admin()
    rescued_session = local_accounts_host.issue_session(account.id)
    with TestClient(local_accounts_host.app) as client:
        response = client.delete(
            f"/api/v1/local-accounts/{account.id}/totp",
            headers={"Authorization": f"Bearer {local_accounts_host.issue_session(admin.id)}"},
        )
        assert response.status_code == 200, response.text
        revoked = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {rescued_session}"},
        )
        assert revoked.status_code == 401

    with SessionLocal() as db:
        row = db.get(Account, uuid.UUID(account.id))
        assert row is not None
        assert row.totp_enabled is False
        assert row.totp_secret is None
        assert row.totp_pending_secret is None

    audits = local_accounts_host.audit_rows(account.id)
    assert [row.action for row in audits] == ["accounts.local.totp.disable"]
    audit_payload = "".join(f"{row.before_data!r}{row.after_data!r}" for row in audits).lower()
    assert active_secret.lower() not in audit_payload
    assert pending_secret.lower() not in audit_payload
