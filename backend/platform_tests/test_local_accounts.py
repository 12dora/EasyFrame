from __future__ import annotations

import os
import uuid

import pyotp
import pytest
from fastapi.testclient import TestClient

from blank_app.database import SessionLocal
from blank_app.models import Account, Notification, Passkey, PermissionCatalog, PlatformAuditLog

# 整模块依赖 blank 宿主已 seed 出 admin;多条用例会直接改库并复用真实登录路由。
pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def _admin_headers(client: TestClient) -> dict[str, str]:
    del client
    with SessionLocal() as db:
        admin = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        admin.must_change_password = False
        admin.active = True
        admin.totp_enabled = False
        admin.totp_secret = None
        admin.totp_pending_secret = None
        db.commit()
        account_id = str(admin.id)
    return _session_headers(account_id)


def _session_headers(account_id: str) -> dict[str, str]:
    from blank_app.adapters import account_adapter

    return {"Authorization": f"Bearer {account_adapter.issue_session(account_id)}"}


def _login_headers(client: TestClient, *, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.json()
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


def _create_local_account(
    *,
    username: str,
    password: str,
    permissions: list[str] | None = None,
) -> str:
    from blank_app.adapters import pwd_context

    with SessionLocal() as db:
        account = Account(
            username=username,
            email=f"{username}@example.com",
            password_hash=pwd_context.hash(password),
            active=True,
            is_admin=False,
            must_change_password=False,
            local_permissions=[{"code": code, "scope": "ALL"} for code in permissions or []],
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return str(account.id)


def _create_manager_account(*, username: str, password: str) -> str:
    return _create_local_account(
        username=username,
        password=password,
        permissions=["accounts.local.view", "accounts.local.manage"],
    )


def test_local_accounts_crud_permissions_audit_and_guards() -> None:
    from blank_app.main import app

    username = f"local-user-{uuid.uuid4().hex[:8]}"
    password = "Local-user-password-42!"
    updated_password = "Local-user-password-43!"

    with TestClient(app) as client:
        headers = _admin_headers(client)

        create = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": username,
                "email": f"{username}@example.com",
                "password": password,
                "mustChangePassword": False,
                "permissions": [
                    {"code": "accounts.local.view", "scope": "ALL"},
                    {"code": "settings.app_setting.update", "scope": "ALL"},
                ],
            },
        )
        assert create.status_code == 201, create.json()
        created = create.json()
        account_id = created["id"]
        assert created["permissions"] == [
            {"code": "accounts.local.view", "scope": "ALL"},
            {"code": "settings.app_setting.update", "scope": "ALL"},
        ]
        with SessionLocal() as db:
            stored = db.get(Account, uuid.UUID(account_id))
            assert stored is not None
            assert stored.local_permissions == [
                {"code": "accounts.local.view", "scope": "ALL"},
                {"code": "settings.app_setting.update", "scope": "ALL"},
            ]
        assert created["baselinePermissions"] == sorted(
            [
                "auth.totp.create",
                "auth.totp.advance",
                "auth.passkey.view",
                "auth.passkey.create",
                "notification.center.view",
            ]
        )

        duplicate = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={"username": username, "password": password},
        )
        assert duplicate.status_code == 409

        listing = client.get(f"/api/v1/local-accounts?search={username}", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["meta"]["total"] == 1
        assert listing.json()["data"][0]["username"] == username

        detail = client.get(f"/api/v1/local-accounts/{account_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["email"] == f"{username}@example.com"

        me_headers = _login_headers(client, username=username, password=password)
        me = client.get("/api/v1/auth/me", headers=me_headers)
        assert me.status_code == 200
        assert set(me.json()["permissions"]) == {
            "accounts.local.view",
            "auth.passkey.create",
            "auth.passkey.view",
            "auth.totp.advance",
            "auth.totp.create",
            "notification.center.view",
            "settings.app_setting.update",
        }
        forbidden_create = client.post(
            "/api/v1/local-accounts",
            headers=me_headers,
            json={"username": f"forbidden-{username}", "password": password},
        )
        assert forbidden_create.status_code == 403

        catalog_disable = client.post(
            "/api/v1/authz-integration/settings",
            headers=headers,
            json={},
        )
        assert catalog_disable.status_code in {405, 422}

        with SessionLocal() as db:
            row = db.get(PermissionCatalog, "settings.app_setting.update")
            assert row is not None
            row.active = False
            db.commit()

        me_after_catalog_disable = client.get("/api/v1/auth/me", headers=me_headers)
        assert me_after_catalog_disable.status_code == 200
        assert "settings.app_setting.update" not in me_after_catalog_disable.json()["permissions"]
        assert "auth.passkey.create" in me_after_catalog_disable.json()["permissions"]
        assert "accounts.local.view" in me_after_catalog_disable.json()["permissions"]

        with SessionLocal() as db:
            row = db.get(PermissionCatalog, "settings.app_setting.update")
            assert row is not None
            row.active = True
            db.commit()

        patch = client.patch(
            f"/api/v1/local-accounts/{account_id}",
            headers=headers,
            json={"email": f"updated-{username}@example.com", "uiLocale": "en-US"},
        )
        assert patch.status_code == 200
        assert patch.json()["email"] == f"updated-{username}@example.com"
        assert patch.json()["uiLocale"] == "en-US"

        permission_update = client.put(
            f"/api/v1/local-accounts/{account_id}/permissions",
            headers=headers,
            json={
                "permissions": [{"code": "accounts.local.manage", "scope": "ALL"}],
                "expectedVersion": created["localGrantsVersion"],
            },
        )
        assert permission_update.status_code == 200
        assert permission_update.json()["permissions"] == [{"code": "accounts.local.manage", "scope": "ALL"}]

        unknown_permissions = client.put(
            f"/api/v1/local-accounts/{account_id}/permissions",
            headers=headers,
            json={
                "permissions": [{"code": "no.such.permission", "scope": "ALL"}],
                "expectedVersion": permission_update.json()["localGrantsVersion"],
            },
        )
        assert unknown_permissions.status_code == 422

        admin_permissions = client.patch(
            f"/api/v1/local-accounts/{account_id}",
            headers=headers,
            json={"isAdmin": True},
        )
        assert admin_permissions.status_code == 200
        assert admin_permissions.json()["isAdmin"] is True
        assert admin_permissions.json()["permissions"] == []

        write_permissions_on_admin = client.put(
            f"/api/v1/local-accounts/{account_id}/permissions",
            headers=headers,
            json={
                "permissions": [{"code": "accounts.local.view", "scope": "ALL"}],
                "expectedVersion": admin_permissions.json()["localGrantsVersion"],
            },
        )
        assert write_permissions_on_admin.status_code == 422

        reset_password = client.post(
            f"/api/v1/local-accounts/{account_id}/password",
            headers=headers,
            json={"password": updated_password, "mustChangePassword": False},
        )
        assert reset_password.status_code == 200
        revoked_session = client.get("/api/v1/auth/me", headers=me_headers)
        assert revoked_session.status_code == 401

        with SessionLocal() as db:
            row = db.get(Account, uuid.UUID(account_id))
            assert row is not None
            row.totp_enabled = True
            row.totp_secret = pyotp.random_base32()
            row.totp_pending_secret = "pending-secret"
            db.commit()

        totp_disable = client.delete(f"/api/v1/local-accounts/{account_id}/totp", headers=headers)
        assert totp_disable.status_code == 200
        with SessionLocal() as db:
            row = db.get(Account, uuid.UUID(account_id))
            assert row is not None
            assert row.totp_enabled is False
            assert row.totp_secret is None
            assert row.totp_pending_secret is None

        deactivate = client.patch(
            f"/api/v1/local-accounts/{account_id}",
            headers=headers,
            json={"active": False},
        )
        assert deactivate.status_code == 200

        inactive_login = client.post("/api/v1/auth/login", json={"username": username, "password": updated_password})
        assert inactive_login.status_code == 401

        reactivate = client.patch(
            f"/api/v1/local-accounts/{account_id}",
            headers=headers,
            json={"active": True, "isAdmin": False},
        )
        assert reactivate.status_code == 200

        with SessionLocal() as db:
            local_account = db.get(Account, uuid.UUID(account_id))
            assert local_account is not None
            db.add(Notification(account_id=local_account.id, title="hello"))
            db.add(
                Passkey(
                    account_id=local_account.id,
                    credential_id=f"cred-{uuid.uuid4().hex}",
                    public_key="public-key",
                    sign_count=0,
                    name="device",
                )
            )
            db.commit()

        delete = client.delete(f"/api/v1/local-accounts/{account_id}", headers=headers)
        assert delete.status_code == 204

        with SessionLocal() as db:
            assert db.get(Account, uuid.UUID(account_id)) is None
            assert db.query(Notification.id).filter(Notification.account_id == uuid.UUID(account_id)).count() == 0
            assert db.query(Passkey.id).filter(Passkey.account_id == uuid.UUID(account_id)).count() == 0
            audits = (
                db.query(PlatformAuditLog)
                .filter(PlatformAuditLog.after_data["targetAccountId"].as_string() == account_id)
                .order_by(PlatformAuditLog.created_at.asc())
                .all()
            )
            actions = [row.action for row in audits]
            assert actions == [
                "accounts.local.create",
                "accounts.local.update",
                "accounts.local.permissions.set",
                "accounts.local.update",
                "accounts.local.password.reset",
                "accounts.local.totp.disable",
                "accounts.local.update",
                "accounts.local.update",
                "accounts.local.delete",
            ]
            for row in audits:
                assert row.after_data is None or "targetAccountId" in row.after_data
                payload = f"{row.before_data!r}{row.after_data!r}".lower()
                assert updated_password.lower() not in payload
                assert "pending-secret" not in payload


def test_local_account_create_validates_password_and_admin_grants() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        short_password = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={"username": f"short-{uuid.uuid4().hex[:8]}", "password": "short"},
        )
        assert short_password.status_code == 422
        admin_grants = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"admin-grants-{uuid.uuid4().hex[:8]}",
                "password": "Admin-password-42!",
                "isAdmin": True,
                "permissions": [{"code": "accounts.local.view", "scope": "ALL"}],
            },
        )
        assert admin_grants.status_code == 422


def test_local_accounts_self_and_last_admin_guards() -> None:
    from blank_app.main import app

    manager_username = f"manager-{uuid.uuid4().hex[:8]}"
    manager_password = "Manager-password-42!"
    _create_manager_account(username=manager_username, password=manager_password)

    with SessionLocal() as db:
        admin = db.query(Account).filter(Account.username == "admin").one()
        admin.must_change_password = False
        admin_id = str(admin.id)
        db.commit()

    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        manager_headers = _login_headers(client, username=manager_username, password=manager_password)

        self_deactivate = client.patch(
            f"/api/v1/local-accounts/{admin_id}", headers=admin_headers, json={"active": False}
        )
        assert self_deactivate.status_code == 403

        self_demote = client.patch(f"/api/v1/local-accounts/{admin_id}", headers=admin_headers, json={"isAdmin": False})
        assert self_demote.status_code == 403

        self_delete = client.delete(f"/api/v1/local-accounts/{admin_id}", headers=admin_headers)
        assert self_delete.status_code == 403

        last_admin_deactivate = client.patch(
            f"/api/v1/local-accounts/{admin_id}",
            headers=manager_headers,
            json={"active": False},
        )
        assert last_admin_deactivate.status_code == 403

        last_admin_demote = client.patch(
            f"/api/v1/local-accounts/{admin_id}",
            headers=manager_headers,
            json={"isAdmin": False},
        )
        assert last_admin_demote.status_code == 422

        last_admin_delete = client.delete(f"/api/v1/local-accounts/{admin_id}", headers=manager_headers)
        assert last_admin_delete.status_code == 403


def test_local_accounts_catalog_and_manifest_include_new_codes() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        catalog = client.get("/api/v1/authz-integration/permission-catalog", headers=headers)
        assert catalog.status_code == 200
        codes = {item["code"] for item in catalog.json()}
        assert {"accounts.local.view", "accounts.local.manage"} <= codes

        manifest = client.get("/api/v1/authz-integration/manifest", headers=headers)
        assert manifest.status_code == 200
        manifest_codes = {item["key"] for item in manifest.json()["permissions"]}
        assert {"accounts.local.view", "accounts.local.manage"} <= manifest_codes


@pytest.mark.parametrize(
    ("method", "path_template", "body", "view_allowed"),
    [
        ("GET", "/api/v1/local-accounts", None, True),
        (
            "POST",
            "/api/v1/local-accounts",
            {"username": "permission-matrix-created", "password": "Matrix-password-42!"},
            False,
        ),
        ("GET", "/api/v1/local-accounts/{account_id}", None, True),
        ("PATCH", "/api/v1/local-accounts/{account_id}", {"email": "matrix@example.com"}, False),
        ("DELETE", "/api/v1/local-accounts/{account_id}", None, False),
        (
            "POST",
            "/api/v1/local-accounts/{account_id}/password",
            {"password": "Matrix-reset-password-42!", "mustChangePassword": False},
            False,
        ),
        (
            "PUT",
            "/api/v1/local-accounts/{account_id}/permissions",
            {"permissions": [{"code": "accounts.local.view", "scope": "ALL"}], "expectedVersion": 0},
            False,
        ),
        ("DELETE", "/api/v1/local-accounts/{account_id}/totp", None, False),
        ("GET", "/api/v1/local-accounts/permission-catalog", None, True),
    ],
    ids=["list", "create", "get", "update", "delete", "password", "permissions", "totp", "catalog"],
)
def test_local_accounts_permission_matrix(
    method: str,
    path_template: str,
    body: dict[str, object] | None,
    view_allowed: bool,
) -> None:
    from blank_app.main import app

    suffix = uuid.uuid4().hex[:8]
    password = "Matrix-account-password-42!"
    no_permissions_username = f"matrix-none-{suffix}"
    view_username = f"matrix-view-{suffix}"
    target_id = _create_local_account(username=f"matrix-target-{suffix}", password=password)
    no_permissions_id = _create_local_account(username=no_permissions_username, password=password)
    view_id = _create_local_account(
        username=view_username,
        password=password,
        permissions=["accounts.local.view"],
    )
    path = path_template.format(account_id=target_id)
    request_kwargs = {"json": body} if body is not None else {}

    with TestClient(app) as client:
        no_permissions_headers = _session_headers(no_permissions_id)
        no_permissions = client.request(method, path, headers=no_permissions_headers, **request_kwargs)
        assert no_permissions.status_code == 403

        view_headers = _session_headers(view_id)
        view_response = client.request(method, path, headers=view_headers, **request_kwargs)
        assert view_response.status_code == (200 if view_allowed else 403)


@pytest.mark.parametrize(
    ("method", "path_template", "body"),
    [
        ("GET", "/api/v1/local-accounts/{account_id}", None),
        ("PATCH", "/api/v1/local-accounts/{account_id}", {"active": False}),
        ("DELETE", "/api/v1/local-accounts/{account_id}", None),
        (
            "POST",
            "/api/v1/local-accounts/{account_id}/password",
            {"password": "External-reset-password-42!"},
        ),
        (
            "PUT",
            "/api/v1/local-accounts/{account_id}/permissions",
            {"permissions": [{"code": "accounts.local.view", "scope": "ALL"}], "expectedVersion": 0},
        ),
        ("DELETE", "/api/v1/local-accounts/{account_id}/totp", None),
    ],
    ids=["get", "update", "delete", "password", "permissions", "totp"],
)
def test_local_account_endpoints_hide_external_accounts(
    method: str,
    path_template: str,
    body: dict[str, object] | None,
) -> None:
    from blank_app.main import app

    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        external = Account(
            username=f"external-{suffix}",
            email=f"external-{suffix}@example.com",
            external_source="easyauth",
            external_user_id=f"external-user-{suffix}",
            active=True,
            is_admin=False,
            must_change_password=False,
            local_permissions=[],
        )
        db.add(external)
        db.commit()
        db.refresh(external)
        external_id = str(external.id)

    with TestClient(app) as client:
        headers = _admin_headers(client)
        request_kwargs = {"json": body} if body is not None else {}
        response = client.request(
            method,
            path_template.format(account_id=external_id),
            headers=headers,
            **request_kwargs,
        )
        assert response.status_code == 404


def test_local_account_grants_reject_inactive_codes_and_do_not_store_baseline() -> None:
    from blank_app.main import app

    suffix = uuid.uuid4().hex[:8]
    inactive_code = f"test.inactive.{suffix}"
    with SessionLocal() as db:
        db.add(
            PermissionCatalog(
                code=inactive_code,
                name_zh="停用权限",
                name_en="Inactive permission",
                domain="test",
                resource="test.inactive",
                supported_scopes=["ALL"],
                risk_level="standard",
                active=False,
            )
        )
        db.commit()

    with TestClient(app) as client:
        headers = _admin_headers(client)
        create = client.post(
            "/api/v1/local-accounts",
            headers=headers,
            json={
                "username": f"baseline-{suffix}",
                "password": "Baseline-password-42!",
                "mustChangePassword": False,
                "permissions": [
                    {"code": "auth.totp.create", "scope": "SELF"},
                    {"code": "accounts.local.view", "scope": "ALL"},
                ],
            },
        )
        assert create.status_code == 201, create.json()
        account_id = create.json()["id"]
        assert create.json()["permissions"] == [{"code": "accounts.local.view", "scope": "ALL"}]
        assert create.json()["permissionCount"] == 1

        detail = client.get(f"/api/v1/local-accounts/{account_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["permissions"] == [{"code": "accounts.local.view", "scope": "ALL"}]
        assert "auth.totp.create" in detail.json()["baselinePermissions"]

        update = client.put(
            f"/api/v1/local-accounts/{account_id}/permissions",
            headers=headers,
            json={
                "permissions": [
                    {"code": "auth.passkey.view", "scope": "SELF"},
                    {"code": "accounts.local.manage", "scope": "ALL"},
                ],
                "expectedVersion": create.json()["localGrantsVersion"],
            },
        )
        assert update.status_code == 200
        assert update.json()["permissions"] == [{"code": "accounts.local.manage", "scope": "ALL"}]
        assert update.json()["permissionCount"] == 1

        updated_detail = client.get(f"/api/v1/local-accounts/{account_id}", headers=headers)
        assert updated_detail.status_code == 200
        assert updated_detail.json()["permissions"] == [{"code": "accounts.local.manage", "scope": "ALL"}]

        inactive = client.put(
            f"/api/v1/local-accounts/{account_id}/permissions",
            headers=headers,
            json={
                "permissions": [{"code": inactive_code, "scope": "ALL"}],
                "expectedVersion": update.json()["localGrantsVersion"],
            },
        )
        assert inactive.status_code == 422


def test_local_account_permission_catalog_is_full_and_available_to_view_only_accounts() -> None:
    from blank_app.main import app

    suffix = uuid.uuid4().hex[:8]
    username = f"catalog-view-{suffix}"
    password = "Catalog-view-password-42!"
    account_id = _create_local_account(
        username=username,
        password=password,
        permissions=["accounts.local.view"],
    )

    with TestClient(app) as client:
        headers = _session_headers(account_id)
        response = client.get("/api/v1/local-accounts/permission-catalog", headers=headers)
        assert response.status_code == 200
        items = response.json()["data"]
        assert items
        assert all(item["code"] and item["nameZh"] and item["nameEn"] and item["groupKey"] for item in items)
        assert all(
            set(item)
            == {
                "code",
                "nameZh",
                "nameEn",
                "groupKey",
                "riskLevel",
                "supportedScopes",
                "grantableScopes",
            }
            for item in items
        )
        local_view = next(item for item in items if item["code"] == "accounts.local.view")
        assert local_view["groupKey"] == "accounts"
        assert local_view["grantableScopes"] == ["ALL"]

        integration_catalog = client.get("/api/v1/authz-integration/permission-catalog", headers=headers)
        assert integration_catalog.status_code == 403
