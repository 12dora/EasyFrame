from __future__ import annotations

import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

from blank_app.database import SessionLocal
from blank_app.models import Account, Passkey, PermissionCatalog, PlatformAuditLog

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def _new_local(*, password: str, is_admin: bool = False, expires_at: datetime | None = None) -> tuple[str, str]:
    from blank_app.adapters import pwd_context

    username = f"batch3-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        account = Account(
            username=username,
            password_hash=pwd_context.hash(password),
            active=True,
            is_admin=is_admin,
            must_change_password=False,
            expires_at=expires_at,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return str(account.id), username


def _new_passwordless_local(*, is_admin: bool) -> tuple[str, str]:
    username = f"batch3-passwordless-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        account = Account(
            username=username,
            password_hash=None,
            active=True,
            is_admin=is_admin,
            must_change_password=False,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return str(account.id), username


def _login(client: TestClient, username: str, password: str, **extra):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password, **extra},
    )


def test_unified_eligibility_invalidates_login_and_existing_sessions(monkeypatch) -> None:
    from blank_app.adapters import account_adapter
    from blank_app.main import app

    password = "Batch3-eligibility-password!"
    normal_id, normal_name = _new_local(password=password)
    admin_id, admin_name = _new_local(password=password, is_admin=True)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")

    with TestClient(app) as client:
        normal_login = _login(client, normal_name, password)
        assert normal_login.status_code == 200
        normal_headers = {"Authorization": f"Bearer {normal_login.json()['accessToken']}"}

        with SessionLocal() as db:
            account = db.get(Account, uuid.UUID(normal_id))
            account.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        expired_login = _login(client, normal_name, password)
        assert expired_login.status_code == 401
        assert expired_login.json()["detail"] == "用户名或密码错误"
        assert client.get("/api/v1/auth/me", headers=normal_headers).status_code == 401

        with SessionLocal() as db:
            account = db.get(Account, uuid.UUID(normal_id))
            account.expires_at = None
            db.commit()
        normal_token = account_adapter.issue_session(normal_id)

        monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "break_glass")
        rejected = _login(client, normal_name, password)
        assert rejected.status_code == 401
        assert rejected.json()["detail"] == "用户名或密码错误"
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {normal_token}"}).status_code == 401
        assert _login(client, admin_name, password).status_code == 200

        monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "disabled")
        assert _login(client, admin_name, password).status_code == 401

        external = Account(
            username=f"batch3-sso-{uuid.uuid4().hex}",
            external_source="authentik",
            external_user_id=uuid.uuid4().hex,
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        with SessionLocal() as db:
            db.add(external)
            db.commit()
            db.refresh(external)
            external_id = str(external.id)
        sso_token = account_adapter.issue_session(external_id)
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {sso_token}"}).status_code == 200

    assert admin_id


def test_password_auth_performs_exactly_one_bcrypt_verify(monkeypatch) -> None:
    from blank_app.adapters import account_adapter, pwd_context

    password = "Batch3-single-bcrypt-password!"
    account_id, username = _new_local(password=password)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    real_verify = pwd_context.verify
    calls = 0

    def counted_verify(secret: str, encoded: str) -> bool:
        nonlocal calls
        calls += 1
        return real_verify(secret, encoded)

    monkeypatch.setattr(pwd_context, "verify", counted_verify)
    for attempted_username, attempted_password in (
        (f"missing-{uuid.uuid4().hex}", password),
        (username, "wrong-password"),
    ):
        before = calls
        assert account_adapter.authenticate_password(attempted_username, attempted_password) is None
        assert calls - before == 1

    with SessionLocal() as db:
        account = db.get(Account, uuid.UUID(account_id))
        account.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    before = calls
    assert account_adapter.authenticate_password(username, password) is None
    assert calls - before == 1


@pytest.mark.parametrize(
    ("mode", "is_admin"),
    [("enabled", False), ("break_glass", True)],
)
def test_passwordless_local_account_cannot_use_timing_equalizer_credential(
    monkeypatch, mode: str, is_admin: bool
) -> None:
    from blank_app.adapters import TIMING_EQUALIZER_HASH, pwd_context
    from blank_app.main import app
    from enterprise_platform.rate_limit import reset_rate_limits

    _, username = _new_passwordless_local(is_admin=is_admin)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", mode)
    reset_rate_limits()
    with TestClient(app) as client:
        response = _login(client, username, "enterprise-platform-timing-equalizer")
    assert pwd_context.verify("enterprise-platform-timing-equalizer", TIMING_EQUALIZER_HASH)
    assert response.status_code == 401


def test_superadmin_projects_full_registry_and_max_supported_scope(monkeypatch) -> None:
    from blank_app.adapters import account_adapter, request_token
    from blank_app.permission_registry import FRAMEWORK_PERMISSIONS
    from enterprise_platform.authz import DataScope

    account_id, _ = _new_local(password="Batch3-superadmin-password!", is_admin=True)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    token = account_adapter.issue_session(account_id)
    context = request_token.set(token)
    try:
        user = account_adapter.current_user()
    finally:
        request_token.reset(context)

    assert set(user.permissions) == {permission.code for permission in FRAMEWORK_PERMISSIONS}
    by_code = {grant.code: grant.scope for grant in user.grants}
    for permission in FRAMEWORK_PERMISSIONS:
        expected = DataScope.ALL if DataScope.ALL in permission.supported_scopes else DataScope.SELF
        assert by_code[permission.code] is expected


@contextmanager
def _temporarily_disable_all_admins():
    with SessionLocal() as db:
        rows = db.query(Account).filter(Account.external_source.is_(None), Account.is_admin.is_(True)).all()
        state = [(row.id, row.active, row.expires_at) for row in rows]
        for row in rows:
            row.active = False
        db.commit()
    try:
        yield
    finally:
        with SessionLocal() as db:
            for account_id, active, expires_at in state:
                row = db.get(Account, account_id)
                if row is not None:
                    row.active = active
                    row.expires_at = expires_at
            db.commit()


@pytest.mark.parametrize("mode", ["development", "demo", "enabled", "break_glass"])
def test_seed_modes_insert_only_when_required(monkeypatch, mode: str) -> None:
    from blank_app.adapters import seed_default_admin

    username = f"batch3-seed-{mode}-{uuid.uuid4().hex}"
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", mode)
    monkeypatch.setenv("BLANK_RUNTIME_ENV", "test")
    monkeypatch.setenv("BLANK_ADMIN_USERNAME", username)
    monkeypatch.setenv("BLANK_ADMIN_PASSWORD", "Batch3-bootstrap-password!")
    scope = _temporarily_disable_all_admins() if mode in {"enabled", "break_glass"} else nullcontext()
    with scope:
        seed_default_admin()
        with SessionLocal() as db:
            seeded = db.query(Account).filter(Account.username == username).one()
            assert seeded.active and seeded.is_admin and seeded.external_source is None
            db.delete(seeded)
            db.commit()


def test_disabled_seed_is_noop_and_enabled_conflict_fails(monkeypatch) -> None:
    from blank_app.adapters import seed_default_admin

    username = f"batch3-disabled-{uuid.uuid4().hex}"
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "disabled")
    monkeypatch.setenv("BLANK_ADMIN_USERNAME", username)
    monkeypatch.delenv("BLANK_ADMIN_PASSWORD", raising=False)
    seed_default_admin()
    with SessionLocal() as db:
        assert db.query(Account.id).filter(Account.username == username).first() is None

    conflict_name = f"batch3-conflict-{uuid.uuid4().hex}"
    _new_local(password="Batch3-conflict-control-password!", is_admin=True)
    with SessionLocal() as db:
        conflict = Account(
            username=conflict_name,
            external_source="authentik",
            external_user_id=uuid.uuid4().hex,
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(conflict)
        db.commit()
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    monkeypatch.setenv("BLANK_ADMIN_USERNAME", conflict_name)
    with pytest.raises(RuntimeError, match="not a usable local superadmin"):
        seed_default_admin()
    with SessionLocal() as db:
        db.query(Account).filter(Account.username == conflict_name).delete()
        db.commit()


def test_enabled_seed_does_not_require_password_when_usable_admin_exists(monkeypatch) -> None:
    from blank_app.adapters import seed_default_admin

    _new_local(password="Batch3-existing-admin-password!", is_admin=True)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    monkeypatch.setenv("BLANK_ADMIN_USERNAME", f"unused-{uuid.uuid4().hex}")
    monkeypatch.delenv("BLANK_ADMIN_PASSWORD", raising=False)
    seed_default_admin()


@pytest.mark.parametrize("mode", ["development", "demo"])
def test_production_rejects_non_production_local_auth_modes(monkeypatch, mode: str) -> None:
    from blank_app.adapters import seed_default_admin

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", mode)
    monkeypatch.setenv("BLANK_RUNTIME_ENV", "production")
    with pytest.raises(RuntimeError, match="development/demo"):
        seed_default_admin()


def test_local_auth_mode_validation_lists_all_five_modes(monkeypatch) -> None:
    from blank_app.adapters import local_auth_mode

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "invalid")
    with pytest.raises(RuntimeError, match="disabled, development, demo, enabled, or break_glass"):
        local_auth_mode()


def test_login_audits_mode_method_and_never_secrets(monkeypatch) -> None:
    import enterprise_platform.rate_limit as rate_limit
    from blank_app.main import app
    from enterprise_platform.rate_limit import Window, reset_rate_limits

    password = "Batch3-audit-secret-password!"
    account_id, username = _new_local(password=password)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    reset_rate_limits()
    with TestClient(app) as client:
        assert _login(client, username, "wrong-password").status_code == 401
        assert _login(client, username, password).status_code == 200

        secret = pyotp.random_base32()
        with SessionLocal() as db:
            account = db.get(Account, uuid.UUID(account_id))
            account.totp_enabled = True
            account.totp_secret = secret
            db.commit()
        current_code = pyotp.TOTP(secret).now()
        wrong_code = "000000" if current_code != "000000" else "111111"
        assert _login(client, username, password, totpCode=wrong_code).status_code == 401
        correct_code = pyotp.TOTP(secret).now()
        assert _login(client, username, password, totpCode=correct_code).status_code == 200

        passkey_failure = client.post(
            "/api/v1/auth/login/passkey/complete",
            json={
                "username": username,
                "password": password,
                "stateToken": "unused-state",
                "credential": {"id": "missing-credential"},
            },
        )
        assert passkey_failure.status_code == 401

        reset_rate_limits()
        monkeypatch.setattr(rate_limit, "LOGIN_ACCOUNT_FAILURES", Window(1, 300))
        assert _login(client, username, "wrong-password").status_code == 401
        assert _login(client, username, "wrong-password").status_code == 429

    with SessionLocal() as db:
        rows = (
            db.query(PlatformAuditLog)
            .filter(PlatformAuditLog.actor_id == username, PlatformAuditLog.action.like("auth.login.%"))
            .all()
        )
    assert {
        "auth.login.success",
        "auth.login.failure",
        "auth.login.second_factor_failure",
        "auth.login.rate_limited",
    }.issubset({row.action for row in rows})
    assert {row.after_data["method"] for row in rows} >= {"password", "totp", "passkey"}
    assert all(row.after_data["mode"] == "enabled" for row in rows)
    serialized = str([(row.before_data, row.after_data) for row in rows])
    assert password not in serialized
    assert secret not in serialized
    assert correct_code not in serialized
    reset_rate_limits()


def test_long_unknown_username_is_truncated_at_audit_boundary(monkeypatch) -> None:
    from blank_app.main import app
    from enterprise_platform.rate_limit import reset_rate_limits

    username = "u" * 101
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    reset_rate_limits()
    with TestClient(app) as client:
        response = _login(client, username, "unknown-password")

    assert response.status_code == 401
    with SessionLocal() as db:
        row = (
            db.query(PlatformAuditLog)
            .filter(
                PlatformAuditLog.actor_id == username[:100],
                PlatformAuditLog.action == "auth.login.failure",
            )
            .order_by(PlatformAuditLog.created_at.desc())
            .first()
        )
    assert row is not None
    assert row.actor_id == username[:100]


def test_login_audit_storage_failure_does_not_change_auth_response(monkeypatch, caplog) -> None:
    import blank_app.adapters as adapters
    from blank_app.main import app
    from enterprise_platform.rate_limit import reset_rate_limits

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    monkeypatch.setattr(
        adapters,
        "record_platform_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    reset_rate_limits()
    with TestClient(app) as client:
        response = _login(client, f"missing-{uuid.uuid4().hex}", "unknown-password")

    assert response.status_code == 401
    assert "login audit write failed" in caplog.text


def test_forced_stripe_collision_does_not_deadlock_totp_login(monkeypatch) -> None:
    import enterprise_platform.rate_limit as rate_limit
    from blank_app.main import app

    password = "Batch3-lock-family-password!"
    account_id, username = _new_local(password=password)
    secret = pyotp.random_base32()
    with SessionLocal() as db:
        account = db.get(Account, uuid.UUID(account_id))
        account.totp_enabled = True
        account.totp_secret = secret
        db.commit()

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    monkeypatch.setattr(rate_limit, "_admission_lock_index", lambda _identifier, _count: 0)
    rate_limit.reset_rate_limits()
    with TestClient(app) as client:
        response = _login(client, username, password, totpCode=pyotp.TOTP(secret).now())
    assert response.status_code == 200


def test_passkey_completion_failures_use_second_factor_bucket_and_structured_audit(monkeypatch) -> None:
    import enterprise_platform.rate_limit as rate_limit
    from blank_app.main import app
    from enterprise_platform.rate_limit import Window

    password = "Batch3-passkey-rate-password!"
    account_id, username = _new_local(password=password)
    with SessionLocal() as db:
        db.add(
            Passkey(
                account_id=uuid.UUID(account_id),
                credential_id=f"registered-{uuid.uuid4().hex}",
                public_key="unused-for-missing-credential",
                name="batch3 regression",
            )
        )
        db.commit()

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    rate_limit.reset_rate_limits()
    with TestClient(app) as client:
        malformed = client.post(
            "/api/v1/auth/login/passkey/complete",
            json={
                "username": username,
                "password": password,
                "stateToken": "unused-state",
                "credential": {},
            },
        )
        assert malformed.status_code == 400

        rate_limit.reset_rate_limits()
        monkeypatch.setattr(rate_limit, "SECOND_FACTOR_ACCOUNT_FAILURES", Window(1, 300))
        payload = {
            "username": username,
            "password": password,
            "stateToken": "unused-state",
            "credential": {"id": "missing-credential"},
        }
        assert client.post("/api/v1/auth/login/passkey/complete", json=payload).status_code == 401
        assert client.post("/api/v1/auth/login/passkey/complete", json=payload).status_code == 429

    with SessionLocal() as db:
        rows = (
            db.query(PlatformAuditLog)
            .filter(
                PlatformAuditLog.actor_id == username,
                PlatformAuditLog.action == "auth.login.second_factor_failure",
            )
            .all()
        )
    assert rows
    assert all(row.after_data["method"] == "passkey" for row in rows)
    rate_limit.reset_rate_limits()


def test_current_user_grants_are_normalized_on_every_read(monkeypatch) -> None:
    from blank_app.adapters import account_adapter, request_token

    password = "Batch3-normalization-password!"
    account_id, _ = _new_local(password=password)
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "enabled")
    with SessionLocal() as db:
        account = db.get(Account, uuid.UUID(account_id))
        account.local_permissions = [
            {"code": "accounts.local.view", "scope": "ALL"},
            {"code": "identity.integration.view", "scope": "all"},
            {"code": "ops.upstream_health.view", "scope": "garbage"},
            {"code": "accounts.local.manage", "scope": "SELF"},
            {"code": "settings.app_setting.update", "scope": "ALL"},
            {"code": "unknown.permission", "scope": "ALL"},
            {"code": "accounts.local.view", "scope": "MANAGED_USERS"},
            {"code": "accounts.local.view"},
            "malformed",
        ]
        inactive = db.get(PermissionCatalog, "settings.app_setting.update")
        inactive.active = False
        db.commit()

    token = account_adapter.issue_session(account_id)
    context = request_token.set(token)
    try:
        user = account_adapter.current_user()
        pairs = {(grant.code, grant.scope.value) for grant in user.grants}
        assert ("accounts.local.view", "ALL") in pairs
        assert ("accounts.local.manage", "SELF") not in pairs
        assert ("accounts.local.view", "MANAGED_USERS") not in pairs
        assert "identity.integration.view" not in user.permissions
        assert "ops.upstream_health.view" not in user.permissions
        assert "settings.app_setting.update" not in user.permissions
        assert "unknown.permission" not in user.permissions
        assert set(user.permissions) == {grant.code for grant in user.grants}
    finally:
        request_token.reset(context)
        with SessionLocal() as db:
            inactive = db.get(PermissionCatalog, "settings.app_setting.update")
            inactive.active = True
            db.commit()
