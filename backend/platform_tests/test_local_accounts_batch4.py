from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blank_app.database import SessionLocal
from blank_app.models import Account, PermissionCatalog, PlatformAuditLog
from enterprise_platform.local_accounts import create_local_accounts_router
from enterprise_platform.schemas import CurrentUser

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

STANDARD = {"code": "accounts.local.view", "scope": "ALL"}
HIGH = {"code": "settings.app_setting.update", "scope": "ALL"}


def _session_headers(account_id: str) -> dict[str, str]:
    from blank_app.adapters import account_adapter

    return {"Authorization": f"Bearer {account_adapter.issue_session(account_id)}"}


def _admin_headers() -> dict[str, str]:
    with SessionLocal() as db:
        account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        account.active = True
        account.is_admin = True
        account.must_change_password = False
        account.expires_at = None
        db.commit()
        return _session_headers(str(account.id))


def _account(*, kind: str = "normal", manager: bool = False) -> str:
    from blank_app.adapters import pwd_context

    suffix = uuid.uuid4().hex[:10]
    permissions: list[dict[str, str]] = []
    if manager:
        permissions = [
            {"code": "accounts.local.view", "scope": "ALL"},
            {"code": "accounts.local.manage", "scope": "ALL"},
        ]
    elif kind == "high":
        permissions = [HIGH]
    with SessionLocal() as db:
        row = Account(
            username=f"batch4-{kind}-{suffix}",
            email=f"batch4-{suffix}@example.com",
            password_hash=pwd_context.hash("Batch4-password-42!"),
            active=True,
            is_admin=kind == "admin",
            must_change_password=False,
            local_permissions=permissions,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return str(row.id)


def _operator_headers(operator: str) -> dict[str, str]:
    if operator == "superadmin":
        return _admin_headers()
    return _session_headers(_account(manager=True))


def _perform(client: TestClient, operation: str, account_id: str, headers: dict[str, str]):
    path = f"/api/v1/local-accounts/{account_id}"
    if operation == "profile":
        return client.patch(path, headers=headers, json={"email": f"changed-{uuid.uuid4().hex[:6]}@example.com"})
    if operation == "active":
        return client.patch(path, headers=headers, json={"active": False})
    if operation == "delete":
        return client.delete(path, headers=headers)
    if operation == "password":
        return client.post(
            f"{path}/password",
            headers=headers,
            json={"password": "Batch4-reset-password-43!", "mustChangePassword": False},
        )
    if operation == "totp":
        return client.delete(f"{path}/totp", headers=headers)
    if operation == "expiry":
        return client.patch(
            path,
            headers=headers,
            json={"expiresAt": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
        )
    permissions = [] if _target_kind(account_id) == "high" else [STANDARD]
    return client.put(
        f"{path}/permissions",
        headers=headers,
        json={"permissions": permissions, "expectedVersion": 0},
    )


def _target_kind(account_id: str) -> str:
    with SessionLocal() as db:
        row = db.get(Account, uuid.UUID(account_id))
        assert row is not None
        if row.is_admin:
            return "admin"
        if any(isinstance(item, dict) and item.get("code") == HIGH["code"] for item in row.local_permissions):
            return "high"
        return "normal"


@pytest.mark.parametrize("operation", ["profile", "active", "delete", "password", "totp", "expiry", "permissions"])
@pytest.mark.parametrize("operator", ["superadmin", "delegated"])
@pytest.mark.parametrize("target", ["normal", "admin", "high"])
def test_operator_target_matrix(operation: str, operator: str, target: str) -> None:
    from blank_app.main import app

    account_id = _account(kind=target)
    headers = _operator_headers(operator)
    if operator == "superadmin":
        expected = 422 if operation == "permissions" and target == "admin" else 200
        if operation == "delete" and target != "admin":
            expected = 204
        elif operation == "delete" and target == "admin":
            expected = 204
        if operation == "expiry" and target == "admin":
            expected = 422
    else:
        expected = 200 if target == "normal" else 403
        if operation == "delete" and target == "normal":
            expected = 204
        if operation == "permissions" and target == "high":
            expected = 422
        if operation == "permissions" and target == "admin":
            expected = 422

    with TestClient(app) as client:
        response = _perform(client, operation, account_id, headers)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("operator", ["superadmin", "delegated"])
def test_create_matrix_and_admin_expiry(operator: str) -> None:
    from blank_app.main import app

    headers = _operator_headers(operator)
    with TestClient(app) as client:
        normal = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"created-{uuid.uuid4().hex[:10]}",
                "password": "Batch4-created-password-42!",
                "permissions": [STANDARD],
            },
        )
        assert normal.status_code == 201, normal.text
        admin = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"admin-{uuid.uuid4().hex[:10]}",
                "password": "Batch4-admin-password-42!",
                "isAdmin": True,
            },
        )
        assert admin.status_code == (201 if operator == "superadmin" else 422)
        admin_expiry = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"expiring-admin-{uuid.uuid4().hex[:10]}",
                "password": "Batch4-admin-password-42!",
                "isAdmin": True,
                "expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        assert admin_expiry.status_code == 422


def test_delegated_is_admin_both_directions_are_422() -> None:
    from blank_app.main import app

    headers = _operator_headers("delegated")
    with TestClient(app) as client:
        promote = client.patch(
            f"/api/v1/local-accounts/{_account()}", headers=headers, json={"isAdmin": True}
        )
        demote = client.patch(
            f"/api/v1/local-accounts/{_account(kind='admin')}", headers=headers, json={"isAdmin": False}
        )
    assert promote.status_code == 422
    assert demote.status_code == 422


@pytest.mark.parametrize("operator", ["superadmin", "delegated"])
@pytest.mark.parametrize("operation", ["deactivate", "delete", "expiry", "password", "totp"])
def test_self_operation_cells(operator: str, operation: str) -> None:
    from blank_app.main import app

    account_id = (
        str(_admin_account_id()) if operator == "superadmin" else _account(manager=True)
    )
    headers = _session_headers(account_id)
    path = f"/api/v1/local-accounts/{account_id}"
    with TestClient(app) as client:
        if operation == "deactivate":
            response = client.patch(path, headers=headers, json={"active": False})
        elif operation == "delete":
            response = client.delete(path, headers=headers)
        elif operation == "expiry":
            response = client.patch(
                path, headers=headers, json={"expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
            )
        elif operation == "password":
            response = client.post(
                f"{path}/password", headers=headers, json={"password": "Batch4-self-password-44!"}
            )
        else:
            response = client.delete(f"{path}/totp", headers=headers)
    assert response.status_code == 403


def _admin_account_id() -> uuid.UUID:
    with SessionLocal() as db:
        row = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        row.active = True
        row.is_admin = True
        row.must_change_password = False
        db.commit()
        return row.id


def test_superadmin_self_demote_is_403() -> None:
    from blank_app.main import app

    account_id = str(_admin_account_id())
    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/local-accounts/{account_id}",
            headers=_session_headers(account_id),
            json={"isAdmin": False},
        )
    assert response.status_code == 403


def test_grant_validation_baseline_stripping_and_high_diff() -> None:
    from blank_app.main import app

    admin_headers = _admin_headers()
    manager_headers = _operator_headers("delegated")
    with TestClient(app) as client:
        duplicate = client.post(
            "/api/v1/local-accounts",
            headers=admin_headers,
            json={
                "username": f"duplicate-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-duplicate-password-42!",
                "permissions": [STANDARD, {"code": STANDARD["code"], "scope": "SELF"}],
            },
        )
        assert duplicate.status_code == 422
        bad_scope = client.post(
            "/api/v1/local-accounts",
            headers=admin_headers,
            json={
                "username": f"scope-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-scope-password-42!",
                "permissions": [{"code": STANDARD["code"], "scope": "SELF"}],
            },
        )
        assert bad_scope.status_code == 422
        baseline = client.post(
            "/api/v1/local-accounts",
            headers=admin_headers,
            json={
                "username": f"baseline-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-baseline-password-42!",
                "permissions": [{"code": "auth.passkey.view", "scope": "SELF"}, STANDARD],
            },
        )
        assert baseline.status_code == 201
        assert baseline.json()["permissions"] == [STANDARD]
        delegated_high_create = client.post(
            "/api/v1/local-accounts",
            headers=manager_headers,
            json={
                "username": f"delegated-high-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-delegated-high-password-42!",
                "permissions": [HIGH],
            },
        )
        assert delegated_high_create.status_code == 422
        high_add = client.put(
            f"/api/v1/local-accounts/{_account()}/permissions",
            headers=manager_headers,
            json={"permissions": [HIGH], "expectedVersion": 0},
        )
        assert high_add.status_code == 422
        high_remove = client.put(
            f"/api/v1/local-accounts/{_account(kind='high')}/permissions",
            headers=manager_headers,
            json={"permissions": [], "expectedVersion": 0},
        )
        assert high_remove.status_code == 422


def test_permissions_cas_stale_and_concurrent_race() -> None:
    from blank_app.main import app

    target = _account()
    headers = _admin_headers()
    with TestClient(app) as client:
        omitted_version = client.put(
            f"/api/v1/local-accounts/{target}/permissions",
            headers=headers,
            json={"permissions": [STANDARD]},
        )
        assert omitted_version.status_code == 422
        first = client.put(
            f"/api/v1/local-accounts/{target}/permissions",
            headers=headers,
            json={"permissions": [STANDARD], "expectedVersion": 0},
        )
        assert first.status_code == 200
        assert first.json()["localGrantsVersion"] == 1
        stale = client.put(
            f"/api/v1/local-accounts/{target}/permissions",
            headers=headers,
            json={"permissions": [], "expectedVersion": 0},
        )
        assert stale.status_code == 409

    race_target = _account()
    with TestClient(app) as client:
        bodies = (
            {"permissions": [STANDARD], "expectedVersion": 0},
            {"permissions": [{"code": "identity.integration.view", "scope": "ALL"}], "expectedVersion": 0},
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda body: client.put(
                        f"/api/v1/local-accounts/{race_target}/permissions", headers=headers, json=body
                    ),
                    bodies,
                )
            )
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_expiry_tristate_promotion_version_and_catalog_projection() -> None:
    from blank_app.main import app

    headers = _admin_headers()
    future = datetime.now(UTC) + timedelta(days=3)
    code = f"batch4.managed-only.{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(
                PermissionCatalog(
                    code=code,
                    name_zh="仅受管范围",
                    name_en="Managed only",
                    domain="batch4",
                    resource="batch4.managed-only",
                    group_key=None,
                    supported_scopes=["MANAGED_USERS"],
                    risk_level="standard",
                    active=True,
                )
            )
            db.commit()
        past = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"past-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-past-password-42!",
                "expiresAt": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )
        assert past.status_code == 422
        created = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"expiry-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-expiry-password-42!",
                "expiresAt": future.isoformat(),
                "permissions": [STANDARD],
            },
        )
        assert created.status_code == 201
        assert created.json()["expired"] is False
        assert created.json()["expiresAt"] is not None
        account_id = created.json()["id"]
        promoted = client.patch(
            f"/api/v1/local-accounts/{account_id}", headers=headers, json={"isAdmin": True}
        )
        assert promoted.status_code == 200
        assert promoted.json()["expiresAt"] is None
        assert promoted.json()["permissions"] == []
        assert promoted.json()["localGrantsVersion"] == created.json()["localGrantsVersion"] + 1
        with SessionLocal() as db:
            audit = (
                db.query(PlatformAuditLog)
                .filter(
                    PlatformAuditLog.action == "accounts.local.update",
                    PlatformAuditLog.after_data["targetAccountId"].as_string() == account_id,
                )
                .order_by(PlatformAuditLog.created_at.desc())
                .first()
            )
            assert audit is not None
            assert "expiresAt" in audit.after_data["changedKeys"]

        explicit_expiry = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"explicit-expiry-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-explicit-expiry-password-42!",
                "expiresAt": future.isoformat(),
            },
        )
        assert explicit_expiry.status_code == 201
        rejected_promotion = client.patch(
            f"/api/v1/local-accounts/{explicit_expiry.json()['id']}",
            headers=headers,
            json={"isAdmin": True, "expiresAt": future.isoformat()},
        )
        assert rejected_promotion.status_code == 422
        catalog = client.get("/api/v1/local-accounts/permission-catalog", headers=headers)
        managed_only = next(item for item in catalog.json()["data"] if item["code"] == code)
        assert managed_only["groupKey"] == "batch4"
        assert managed_only["supportedScopes"] == ["MANAGED_USERS"]
        assert managed_only["grantableScopes"] == []


def test_naive_expiry_is_rejected_on_create_and_update() -> None:
    from blank_app.main import app

    headers = _admin_headers()
    with TestClient(app) as client:
        create = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"naive-expiry-{uuid.uuid4().hex[:8]}",
                "password": "Batch4-naive-expiry-password-42!",
                "expiresAt": "2030-01-01T00:00:00",
            },
        )
        update = client.patch(
            f"/api/v1/local-accounts/{_account()}",
            headers=headers,
            json={"expiresAt": "2030-01-01T00:00:00"},
        )
    assert create.status_code == 422
    assert update.status_code == 422


def test_permissions_audit_records_code_and_scope_diff_sets() -> None:
    from blank_app.main import app

    target = _account()
    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/local-accounts/{target}/permissions",
            headers=_admin_headers(),
            json={"permissions": [STANDARD], "expectedVersion": 0},
        )
    assert response.status_code == 200
    with SessionLocal() as db:
        audit = (
            db.query(PlatformAuditLog)
            .filter(
                PlatformAuditLog.action == "accounts.local.permissions.set",
                PlatformAuditLog.after_data["targetAccountId"].as_string() == target,
            )
            .order_by(PlatformAuditLog.created_at.desc())
            .first()
        )
        assert audit is not None
        assert audit.before_data["permissions"] == []
        assert audit.after_data["permissions"] == [STANDARD]


def test_audit_failure_rolls_back_business_change(monkeypatch) -> None:
    from blank_app.adapters import BlankLocalAccountUnitOfWork
    from blank_app.main import app

    target = _account()
    with SessionLocal() as db:
        before = db.get(Account, uuid.UUID(target)).email
        audit_count = db.query(PlatformAuditLog.id).count()

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(BlankLocalAccountUnitOfWork, "append_audit", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.patch(
            f"/api/v1/local-accounts/{target}",
            headers=_admin_headers(),
            json={"email": "must-rollback@example.com"},
        )
    assert response.status_code == 500
    with SessionLocal() as db:
        assert db.get(Account, uuid.UUID(target)).email == before
        assert db.query(PlatformAuditLog.id).count() == audit_count


def _kernel_app(actor_id: str) -> FastAPI:
    from blank_app.adapters import BlankLocalAccountAdmin

    app = FastAPI()
    app.include_router(
        create_local_accounts_router(
            BlankLocalAccountAdmin(),
            current_user_dependency=lambda: CurrentUser(
                id=actor_id,
                account_id=actor_id,
                name="kernel-superadmin",
                is_local_superadmin=True,
            ),
            permission_dependency_factory=lambda _code: lambda: None,
        ),
        prefix="/api/v1",
    )
    return app


@pytest.mark.parametrize("operation", ["deactivate", "demote", "delete"])
def test_last_admin_invariant(operation: str) -> None:
    target = _account(kind="admin")
    with SessionLocal() as db:
        db.query(Account).filter(Account.id != uuid.UUID(target), Account.is_admin.is_(True)).update(
            {Account.is_admin: False}
        )
        db.commit()
    app = _kernel_app(str(uuid.uuid4()))
    with TestClient(app) as client:
        path = f"/api/v1/local-accounts/{target}"
        if operation == "deactivate":
            response = client.patch(path, json={"active": False})
        elif operation == "demote":
            response = client.patch(path, json={"isAdmin": False})
        else:
            response = client.delete(path)
    assert response.status_code == 422
    with SessionLocal() as db:
        row = db.get(Account, uuid.UUID(target))
        assert row is not None and row.active and row.is_admin
        seed = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        seed.is_admin = True
        seed.active = True
        db.commit()


def test_concurrent_admin_changes_preserve_one_usable_admin() -> None:
    first, second = _account(kind="admin"), _account(kind="admin")
    with SessionLocal() as db:
        db.query(Account).filter(
            Account.id.not_in([uuid.UUID(first), uuid.UUID(second)]), Account.is_admin.is_(True)
        ).update({Account.is_admin: False})
        db.commit()
    app = _kernel_app(str(uuid.uuid4()))
    with TestClient(app) as client:
        calls = (
            lambda: client.patch(f"/api/v1/local-accounts/{first}", json={"isAdmin": False}),
            lambda: client.delete(f"/api/v1/local-accounts/{second}"),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda call: call(), calls))
    statuses = [response.status_code for response in responses]
    assert sum(status in {200, 204} for status in statuses) == 1
    assert statuses.count(422) == 1
    with SessionLocal() as db:
        usable = db.query(Account).filter(
            Account.external_source.is_(None), Account.active.is_(True), Account.is_admin.is_(True)
        ).count()
        assert usable >= 1
        seed = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        seed.is_admin = True
        seed.active = True
        db.commit()


def test_auth_me_exposes_server_derived_identity_flags() -> None:
    from blank_app.main import app

    account_id = str(_admin_account_id())
    with TestClient(app) as client:
        response = client.get("/api/v1/auth/me", headers=_session_headers(account_id))
    assert response.status_code == 200
    assert response.json()["accountId"] == account_id
    assert response.json()["isLocalSuperadmin"] is True


def test_auth_me_local_admin_is_not_superadmin_when_local_auth_disabled(monkeypatch) -> None:
    from blank_app.adapters import account_adapter
    from blank_app.main import app

    account_id = _admin_account_id()
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        db.expunge(account)

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "disabled")
    monkeypatch.setattr(account_adapter, "_authenticated_account", lambda: (account, False))
    with TestClient(app) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["accountId"] == str(account_id)
    assert response.json()["isLocalSuperadmin"] is False


def test_admin_password_paths_reject_public_example_markers() -> None:
    from blank_app.main import app

    # 超管口令三条路径共用 bootstrap 黑名单(06 §2):公共示例标记必须 422。
    marker_password = "replace-with-a-strong-initial-password"
    admin_headers = _admin_headers()
    other_admin = _account(kind="admin")
    with TestClient(app) as client:
        reset = client.post(
            f"/api/v1/local-accounts/{other_admin}/password",
            headers=admin_headers,
            json={"password": marker_password, "mustChangePassword": False},
        )
        assert reset.status_code == 422

        # 超管自助改密同样受 bootstrap 强度约束。
        self_change = client.post(
            "/api/v1/users/me/password",
            headers=_session_headers(other_admin),
            json={"currentPassword": "Batch4-password-42!", "newPassword": marker_password},
        )
        assert self_change.status_code == 422

        # 普通账户仍走共享口令策略(8-128),不被超管强度波及。
        normal = _account()
        normal_change = client.post(
            "/api/v1/users/me/password",
            headers=_session_headers(normal),
            json={"currentPassword": "Batch4-password-42!", "newPassword": "normal-pass-9"},
        )
        assert normal_change.status_code == 200
