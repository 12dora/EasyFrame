from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import JWTError, jwt
from pydantic import ValidationError

from blank_app.database import SessionLocal
from blank_app.models import (
    Account,
    DescriptorKey,
    Notification,
    Passkey,
    PermissionSnapshot,
    PlatformAuditLog,
    PlatformSetting,
)
from enterprise_platform.auth import AuthError
from enterprise_platform.authz import EasyAuthClientError, EasyAuthPermissionSnapshot
from enterprise_platform.safe_http import UnsafeOutboundUrlError
from enterprise_platform.schemas import ConnectionTestResult, EasyAuthSettingsUpdate, OidcSettingsUpdate
from enterprise_platform.secrets import decrypt_secret, encrypt_secret

# 整模块依赖 blank 宿主已 seed 出 admin;多条用例在进入 TestClient 之前就直接查/改 admin。
pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def _admin_headers(client: TestClient) -> dict[str, str]:
    from enterprise_platform.rate_limit import reset_rate_limits

    reset_rate_limits()
    with SessionLocal() as db:
        admin = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        admin.must_change_password = False
        db.commit()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


class FakeEasyAuthClient:
    fail = False

    def fetch_permission_snapshot(self, user_id: str) -> EasyAuthPermissionSnapshot:
        if self.fail:
            raise EasyAuthClientError("EasyAuth permission query failed")
        return EasyAuthPermissionSnapshot.model_validate(
            {
                "user_id": user_id,
                "app_key": "enterprise-blank",
                "groups": [{"key": "operators", "kind": "role", "name": "Operators"}],
                "grants": [
                    {
                        "permission": "authz.integration.view",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    },
                    {
                        "permission": "unknown.permission",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    },
                ],
                "grant_version": 2,
                "catalog_version": 3,
                "snapshot_version": "2.3",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            }
        )


def oidc_state_key() -> str:
    """OIDC state cookie 的派生签名密钥(与 session 密钥分离)。"""

    from blank_app.adapters import OIDC_STATE_KEY_PURPOSE, signing_key

    return signing_key(OIDC_STATE_KEY_PURPOSE)


def _valid_rsa_jwk() -> dict[str, str]:
    modulus = bytes([0x80]) + bytes(range(1, 256))
    return {
        "kid": "blank-private-hop",
        "kty": "RSA",
        "n": base64.urlsafe_b64encode(modulus).rstrip(b"=").decode(),
        "e": "AQAB",
        "use": "sig",
        "alg": "RS256",
    }


def test_snapshot_isolates_one_malformed_grant() -> None:
    snapshot = EasyAuthPermissionSnapshot.model_validate(
        {
            "user_id": "user-1",
            "app_key": "enterprise-blank",
            "groups": [],
            "grants": [
                {"permission": "authz.integration.view", "scope": "ALL", "source_type": "direct", "source_key": ""},
                {"scope": "ALL"},
            ],
            "grant_version": 1,
            "catalog_version": 1,
            "snapshot_version": "1.1",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
    )
    assert [grant.permission for grant in snapshot.grants] == ["authz.integration.view"]


def test_notification_composite_cursor_has_no_gaps_or_duplicates() -> None:
    from blank_app.adapters import BlankNotificationAdapter

    account_id = None
    created_at = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)
    with SessionLocal() as db:
        account = Account(
            username="notification-cursor-user",
            email=None,
            password_hash=None,
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(account)
        db.flush()
        account_id = account.id
        ids = [uuid.UUID(int=index) for index in range(1, 6)]
        db.add_all(
            Notification(
                id=notification_id,
                account_id=account.id,
                title=f"item-{notification_id.int}",
                created_at=created_at,
            )
            for notification_id in ids
        )
        db.commit()

    adapter = BlankNotificationAdapter()
    cursor = None
    observed: list[str] = []
    while True:
        page = adapter.list_notifications(str(account_id), cursor=cursor, limit=2)
        observed.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert observed == [str(notification_id) for notification_id in reversed(ids)]
    assert len(observed) == len(set(observed))
    with pytest.raises(AuthError, match="通知游标无效"):
        adapter.list_notifications(str(account_id), cursor="not-a-cursor", limit=2)


def test_authority_changes_require_reentering_saved_credentials() -> None:
    from blank_app.adapters import BlankIntegrationAdapter

    adapter = BlankIntegrationAdapter()
    with SessionLocal() as db:
        db.merge(
            PlatformSetting(
                key="oidc",
                value={
                    "issuer": "https://old-id.example.com/",
                    "authorization_endpoint": "https://old-id.example.com/authorize",
                    "token_endpoint": "https://old-id.example.com/token",
                    "jwks_uri": "https://old-id.example.com/jwks",
                    "client_id": "old-app",
                    "client_secret": "must-not-leak",
                    "authentik_api_base_url": "https://old-id.example.com/api",
                    "authentik_api_token": "must-not-leak-either",
                },
            )
        )
        db.merge(
            PlatformSetting(
                key="easyauth",
                value={
                    "base_url": "https://old-auth.example.com",
                    "app_key": "old-app",
                    "credential": "must-not-leak",
                },
            )
        )
        db.commit()
    oidc_payload = OidcSettingsUpdate.model_validate(
        {
            "enabled": True,
            "issuer": "https://new-id.example.com/",
            "authorizationEndpoint": "https://new-id.example.com/authorize",
            "tokenEndpoint": "https://new-id.example.com/token",
            "jwksUri": "https://new-id.example.com/jwks",
            "userinfoEndpoint": "",
            "clientId": "new-app",
            "scopes": "openid profile email",
            "redirectBaseUrl": "http://localhost:8100",
            "frontendBaseUrl": "http://localhost:3100",
            "authentikApiBaseUrl": "https://new-id.example.com/api",
        }
    )
    with pytest.raises(AuthError, match="clientSecret"):
        adapter.save_oidc_settings(oidc_payload, actor_id="test")

    oidc_payload = oidc_payload.model_copy(update={"client_secret": "new-client-secret"})
    with pytest.raises(AuthError, match="API token"):
        adapter.save_oidc_settings(oidc_payload, actor_id="test")

    with pytest.raises(AuthError, match="credential"):
        adapter.save_easyauth_settings(
            EasyAuthSettingsUpdate.model_validate({"baseUrl": "https://new-auth.example.com", "appKey": "new-app"}),
            actor_id="test",
        )
    with SessionLocal() as db:
        assert db.get(PlatformSetting, "oidc").value["client_secret"] == "must-not-leak"
        assert db.get(PlatformSetting, "oidc").value["authentik_api_token"] == "must-not-leak-either"
        assert db.get(PlatformSetting, "easyauth").value["credential"] == "must-not-leak"


def test_oidc_normalization_preserves_credentials_for_semantically_same_authority() -> None:
    from blank_app.adapters import BlankIntegrationAdapter

    client_secret = "preserved-client-secret"
    api_token = "preserved-authentik-token"
    with SessionLocal() as db:
        db.merge(
            PlatformSetting(
                key="oidc",
                value={
                    "enabled": True,
                    "issuer": "  https://identity.example.com///  ",
                    "authorization_endpoint": "  https://identity.example.com/authorize  ",
                    "token_endpoint": " https://identity.example.com/token ",
                    "jwks_uri": " https://identity.example.com/jwks ",
                    "userinfo_endpoint": " https://identity.example.com/userinfo ",
                    "client_id": " enterprise-blank ",
                    "client_secret": encrypt_secret(client_secret),
                    "scopes": "openid profile email",
                    "redirect_base_url": "http://localhost:8100",
                    "frontend_base_url": "http://localhost:3100",
                    "authentik_api_base_url": " https://identity.example.com/api/ ",
                    "authentik_api_token": encrypt_secret(api_token),
                },
            )
        )
        db.commit()

    result = BlankIntegrationAdapter().save_oidc_settings(
        OidcSettingsUpdate.model_validate(
            {
                "enabled": True,
                "issuer": " https://identity.example.com ",
                "authorizationEndpoint": "https://identity.example.com/authorize",
                "tokenEndpoint": "  https://identity.example.com/token  ",
                "jwksUri": "https://identity.example.com/jwks",
                "userinfoEndpoint": " https://identity.example.com/userinfo ",
                "clientId": "  enterprise-blank  ",
                "scopes": "  openid   profile\t email ",
                "redirectBaseUrl": " http://localhost:8100/ ",
                "frontendBaseUrl": " http://localhost:3100/ ",
                "authentikApiBaseUrl": "https://identity.example.com/api",
            }
        ),
        actor_id="normalization-test",
    )

    assert result.issuer == "https://identity.example.com/"
    assert result.authorization_endpoint == "https://identity.example.com/authorize"
    assert result.token_endpoint == "https://identity.example.com/token"
    assert result.jwks_uri == "https://identity.example.com/jwks"
    assert result.userinfo_endpoint == "https://identity.example.com/userinfo"
    assert result.client_id == "enterprise-blank"
    assert result.scopes == "openid profile email"
    assert result.redirect_base_url == "http://localhost:8100"
    assert result.frontend_base_url == "http://localhost:3100"
    assert result.authentik_api_base_url == "https://identity.example.com/api"
    assert result.has_client_secret is True
    assert result.has_authentik_api_token is True
    with SessionLocal() as db:
        stored = db.get(PlatformSetting, "oidc").value
        assert decrypt_secret(stored["client_secret"]) == client_secret
        assert decrypt_secret(stored["authentik_api_token"]) == api_token


def test_integration_credentials_are_encrypted_and_key_loss_fails_closed(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.adapters import BlankIntegrationAdapter

    credential = "one-time-easyauth-test-credential"
    adapter = BlankIntegrationAdapter()
    status = adapter.save_easyauth_settings(
        EasyAuthSettingsUpdate.model_validate(
            {
                "baseUrl": "https://authz-secure.example.com",
                "appKey": "enterprise-blank",
                "credential": credential,
            }
        ),
        actor_id="test-actor",
    )
    assert status.has_credential is True
    with SessionLocal() as db:
        stored = db.get(PlatformSetting, "easyauth").value["credential"]
        assert stored.startswith("enc:v1:")
        assert credential not in stored
        audit = (
            db.query(PlatformAuditLog)
            .filter(PlatformAuditLog.action == "authz.settings.update")
            .order_by(PlatformAuditLog.created_at.desc())
            .first()
        )
        assert audit.after_data["credential"] == "[configured]"

    client = authz_api._permission_client()
    assert client.credential == credential
    client.close()
    monkeypatch.delenv("BLANK_INTEGRATION_ENVELOPE_KEY")
    with pytest.raises(EasyAuthClientError, match="credential storage"):
        authz_api._permission_client()


def test_bootstrap_secret_and_integration_url_guards() -> None:
    from blank_app.adapters import is_unsafe_bootstrap_secret

    assert is_unsafe_bootstrap_secret("replace-with-at-least-32-random-bytes", min_length=32)
    assert is_unsafe_bootstrap_secret("replace-with-a-strong-initial-password", min_length=12)
    assert is_unsafe_bootstrap_secret("short", min_length=12)
    assert not is_unsafe_bootstrap_secret("unique-test-password-42!", min_length=12)
    with pytest.raises(ValidationError, match="非本机地址必须使用 https"):
        OidcSettingsUpdate.model_validate(
            {
                "enabled": True,
                "issuer": "https://identity.example.com/",
                "authorizationEndpoint": "https://identity.example.com/authorize",
                "tokenEndpoint": "https://identity.example.com/token",
                "jwksUri": "https://identity.example.com/jwks",
                "clientId": "enterprise-blank",
                "scopes": "openid",
                "redirectBaseUrl": "http://public.example.com",
                "frontendBaseUrl": "https://app.example.com",
            }
        )


def test_principal_configuration_fails_at_startup(monkeypatch) -> None:
    from blank_app.authz_api import validate_principal_config

    monkeypatch.setenv("BLANK_PRINCIPAL_MODE", "unsupported")
    with pytest.raises(RuntimeError, match="must be one of"):
        validate_principal_config()

    monkeypatch.setenv("BLANK_PRINCIPAL_MODE", "jwt")
    monkeypatch.delenv("BLANK_PRINCIPAL_ISSUER", raising=False)
    monkeypatch.delenv("BLANK_PRINCIPAL_AUDIENCE", raising=False)
    monkeypatch.delenv("BLANK_PRINCIPAL_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="incomplete"):
        validate_principal_config()

    monkeypatch.setenv("BLANK_PRINCIPAL_ISSUER", "https://identity.example.com/")
    monkeypatch.setenv("BLANK_PRINCIPAL_AUDIENCE", "enterprise-blank")
    monkeypatch.setenv("BLANK_PRINCIPAL_SECRET", "replace-with-at-least-32-random-bytes")
    with pytest.raises(RuntimeError, match="public example"):
        validate_principal_config()

    monkeypatch.setenv("BLANK_PRINCIPAL_SECRET", "principal-secret-for-tests-at-least-32-bytes")
    validate_principal_config()


@pytest.mark.parametrize("weak_secret", ["", "short-secret", "replace-with-a-long-random-secret"])
def test_signing_secret_gate_applies_when_local_auth_disabled(monkeypatch, weak_secret) -> None:
    """OIDC-only 部署(本地认证禁用)也必须在启动时拒绝弱/公开示例签名密钥。"""

    from blank_app.main import app

    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "disabled")
    monkeypatch.setenv("BLANK_JWT_SECRET", weak_secret)
    with pytest.raises(RuntimeError, match="BLANK_JWT_SECRET"), TestClient(app):
        pass

    monkeypatch.setenv("BLANK_JWT_SECRET", "compliant-signing-root-key-for-tests-0123")
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_rotated_previous_signing_secret_must_also_be_strong(monkeypatch) -> None:
    """轮换期历史根密钥同样受门禁约束,不能用弱密钥延长可伪造窗口。"""

    from blank_app.adapters import validate_signing_secrets

    monkeypatch.setenv("BLANK_JWT_SECRET", "primary-signing-root-key-for-tests-32b")
    monkeypatch.setenv("BLANK_JWT_SECRET_PREVIOUS", "weak")
    with pytest.raises(RuntimeError, match="BLANK_JWT_SECRET_PREVIOUS"):
        validate_signing_secrets()

    monkeypatch.setenv("BLANK_JWT_SECRET_PREVIOUS", "previous-signing-root-key-for-tests-32b")
    validate_signing_secrets()


def test_session_and_oidc_state_keys_are_separated_and_rotatable(monkeypatch) -> None:
    """session 与 OIDC state 使用不同派生密钥;轮换后旧 session token 仍可用。"""

    import blank_app.adapters as adapters
    from blank_app.main import app

    monkeypatch.setenv("BLANK_JWT_SECRET", "rotation-root-key-one-for-tests-0123456789")
    monkeypatch.delenv("BLANK_JWT_SECRET_PREVIOUS", raising=False)

    session_key = adapters.signing_key(adapters.SESSION_KEY_PURPOSE)
    oidc_key = adapters.signing_key(adapters.OIDC_STATE_KEY_PURPOSE)
    passkey_key = adapters.signing_key(adapters.PASSKEY_STATE_KEY_PURPOSE)
    assert len({session_key, oidc_key, passkey_key, os.environ["BLANK_JWT_SECRET"]}) == 4

    with SessionLocal() as db:
        account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        account.sessions_revoked_at = None
        db.commit()
        account_id = str(account.id)

    session_token = adapters.account_adapter.issue_session(account_id)
    # 用 OIDC state 密钥伪造的 token 不能当 session token 用。
    forged = jwt.encode(
        {"sub": account_id, "auth_source": "local", "iat": datetime.now(UTC), "session_started_at": 0.0},
        oidc_key,
        algorithm=adapters.JWT_ALGORITHM,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"}).status_code == 200
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

    # 反向:session token 也不能通过 OIDC state 密钥验签。
    with pytest.raises(JWTError):
        jwt.decode(session_token, oidc_key, algorithms=[adapters.JWT_ALGORITHM])

    # 轮换根密钥:旧 session token 通过历史密钥继续验签。
    monkeypatch.setenv("BLANK_JWT_SECRET", "rotation-root-key-two-for-tests-0123456789")
    monkeypatch.setenv("BLANK_JWT_SECRET_PREVIOUS", "rotation-root-key-one-for-tests-0123456789")
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"}).status_code == 200

    monkeypatch.delenv("BLANK_JWT_SECRET_PREVIOUS", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"}).status_code == 401


def test_blank_authz_catalog_manifest_snapshot_refresh_and_fail_closed(monkeypatch) -> None:
    import blank_app.authz_api as authz_api
    from blank_app.main import app

    fake = FakeEasyAuthClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    with SessionLocal() as db:
        # 下面的 discovery 断言要求走 SSRF 防护的 guarded_request:一旦持久化的 oidc 设置里
        # 留着 server_base_url,adapters.discover_oidc 会改用 trusted authority transport,
        # errorKind 从 blocked 变成 unreachable。前置状态必须自己清干净。
        db.query(PlatformSetting).filter(PlatformSetting.key == "oidc").delete()
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {
            "configured": True,
            "base_url": "https://auth.example.com",
            "app_key": "enterprise-blank",
            "auth_mode": "static_app_token",
            "has_credential": True,
            "credential": "opaque-test-token",
        }
        # 用例自带唯一主体:快照表是全套用例共享的，固定用户名会让「本用例只产生一条快照」
        # 这个断言依赖执行顺序(先跑的用例会把总数抬高);external_user_id 也必须唯一，
        # 否则会撞上 test_trusted_principal_* 复用的 authentik-user-1 唯一索引。
        external_username = f"external-user-{uuid.uuid4().hex[:8]}"
        external = Account(
            username=external_username,
            email=f"{external_username}@example.com",
            password_hash=None,
            external_source="authentik",
            external_user_id=f"authentik-{external_username}",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add_all([setting, external])
        db.commit()
        db.refresh(external)
        external_id = str(external.id)

    with TestClient(app) as client:
        headers = _admin_headers(client)
        status = client.get("/api/v1/authz-integration/status", headers=headers)
        assert status.status_code == 200
        assert set(status.json()) == {"easyauth", "principal", "catalog", "snapshots"}
        assert status.json()["catalog"]["activeCount"] >= 11

        catalog = client.get("/api/v1/authz-integration/permission-catalog", headers=headers)
        assert catalog.status_code == 200
        assert {item["code"] for item in catalog.json()} >= {
            "auth.passkey.create",
            "authz.integration.manage",
        }

        manifest = client.get("/api/v1/authz-integration/manifest", headers=headers)
        assert manifest.status_code == 200
        assert manifest.json()["app"]["app_key"] == "enterprise-blank"

        grants = client.get("/api/v1/authz-integration/my-grants", headers=headers)
        assert grants.status_code == 200
        assert any(item["permission"] == "authz.integration.manage" for item in grants.json())

        discovery = client.post(
            "/api/v1/identity-integration/discover",
            headers=headers,
            json={"issuer": "https://169.254.169.254"},
        )
        assert discovery.status_code == 200
        assert discovery.json()["errorKind"] == "blocked"
        user_sync = client.post("/api/v1/identity-integration/user-sync", headers=headers)
        assert user_sync.status_code == 200
        assert user_sync.json()["supported"] is False
        assert user_sync.json()["status"] == "not_supported"

        created_key = client.post(
            "/api/v1/authz-integration/descriptor-keys", headers=headers, json={"name": "deployment sync"}
        )
        assert created_key.status_code == 201
        descriptor_id = created_key.json()["key"]["id"]
        assert created_key.json()["token"].startswith("epd_")
        listed_keys = client.get("/api/v1/authz-integration/descriptor-keys", headers=headers)
        assert listed_keys.json()[0]["id"] == descriptor_id
        assert "token" not in listed_keys.json()[0]
        assert (
            client.patch(
                f"/api/v1/authz-integration/descriptor-keys/{descriptor_id}",
                headers=headers,
                json={"active": False},
            ).status_code
            == 200
        )
        assert (
            client.delete(f"/api/v1/authz-integration/descriptor-keys/{descriptor_id}", headers=headers).status_code
            == 204
        )

        refreshed = client.post(f"/api/v1/authz-integration/snapshots/{external_id}/refresh", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["grantCount"] == 1
        # 把查询限定到本用例自己的主体后，「恰好一条」才是被测行为而不是执行顺序的副产品。
        snapshots = client.get(
            f"/api/v1/authz-integration/snapshots?limit=1&offset=0&sort=fetched_at_desc&search={external_username}",
            headers=headers,
        )
        assert snapshots.headers["X-Total-Count"] == "1"
        assert snapshots.json()[0]["snapshotVersion"] == "2.3"
        assert snapshots.json()[0]["roleGroups"] == ["Operators"]

        connection = client.post("/api/v1/authz-integration/connection-test", headers=headers)
        assert connection.status_code == 200
        assert connection.json()["error"] is None
        assert connection.json()["snapshotVersion"] == "2.3"

        fake.fail = True
        failed_connection = client.post("/api/v1/authz-integration/connection-test", headers=headers)
        assert failed_connection.status_code == 200
        assert failed_connection.json()["ok"] is False
        failed = client.post(f"/api/v1/authz-integration/snapshots/{external_id}/refresh", headers=headers)
        assert failed.status_code == 503
        with SessionLocal() as db:
            persisted = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == external_id).one()
            assert persisted.snapshot_version == "2.3"
            assert db.query(DescriptorKey).count() == 0
            actions = {row.action for row in db.query(PlatformAuditLog).all()}
            assert {
                "authz.descriptor_key.create",
                "authz.descriptor_key.update",
                "authz.descriptor_key.delete",
            } <= actions
            refresh_audits = [
                row for row in db.query(PlatformAuditLog).all() if row.action == "authz_integration.snapshot.refresh"
            ]
            manifest_audits = [
                row for row in db.query(PlatformAuditLog).all() if row.action == "authz_integration.manifest.export"
            ]
            assert len(refresh_audits) == 1
            assert len(manifest_audits) == 1
            assert set(refresh_audits[0].after_data) == {"snapshotVersion", "grantCount"}
            assert set(manifest_audits[0].after_data) == {"schemaVersion", "permissionCount"}
            assert "credential" not in str(refresh_audits + manifest_audits).lower()
            connection_audits = (
                db.query(PlatformAuditLog)
                .filter(PlatformAuditLog.action == "authz.connection_test")
                .order_by(PlatformAuditLog.created_at.desc())
                .limit(2)
                .all()
            )
            assert {row.after_data["ok"] for row in connection_audits} == {True, False}
            assert all(set(row.after_data) == {"ok", "latencyMs", "errorKind"} for row in connection_audits)


def test_external_account_snapshot_bootstrap_retries_and_refreshes_expired(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    fake = FakeEasyAuthClient()
    monkeypatch.setattr(authz_api, "_permission_client", lambda: fake)
    with SessionLocal() as db:
        account = Account(
            username="snapshot-retry-user",
            email="retry@example.com",
            password_hash=None,
            external_source="authentik",
            external_user_id="snapshot-retry-user-1",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        account_id = account.id

    fake.fail = True
    assert authz_api.ensure_account_snapshot(account_id) is False
    with SessionLocal() as db:
        assert db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account_id).count() == 0

    fake.fail = False
    assert authz_api.ensure_account_snapshot(account_id) is True
    with SessionLocal() as db:
        snapshot = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account_id).one()
        snapshot.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    assert authz_api.ensure_account_snapshot(account_id) is True
    with SessionLocal() as db:
        snapshot = db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account_id).one()
        assert snapshot.expires_at > datetime.now(UTC)


def test_expired_snapshot_denies_protected_permission_fail_closed() -> None:
    from blank_app.adapters import account_adapter, request_token, require_permission

    with SessionLocal() as db:
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {"app_key": "enterprise-blank"}
        account = Account(
            username="expired-permission-user",
            email="expired@example.com",
            password_hash=None,
            external_source="authentik",
            external_user_id="expired-permission-user-1",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add_all([setting, account])
        db.flush()
        db.add(
            PermissionSnapshot(
                account_id=account.id,
                external_source="authentik",
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
                snapshot_version="expired",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db.commit()
        account_id = str(account.id)

    token = account_adapter.issue_session(account_id)
    context_token = request_token.set(token)
    try:
        assert account_adapter.current_user().permissions == []
        with pytest.raises(AuthError) as captured:
            require_permission("authz.integration.view")
        assert captured.value.status_code == 403
    finally:
        request_token.reset(context_token)


def test_snapshot_unique_conflict_is_recovered_as_update() -> None:
    from blank_app.authz_api import _commit_snapshot_row

    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    with SessionLocal() as db:
        account = Account(
            username="snapshot-conflict-user",
            email=None,
            password_hash=None,
            external_source="authentik",
            external_user_id="snapshot-conflict-user-1",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(account)
        db.flush()
        existing = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id="snapshot-conflict-user-1",
            app_key="enterprise-blank",
            groups=[],
            grants=[],
            grant_version=1,
            catalog_version=1,
            snapshot_version="old",
            expires_at=expires_at,
        )
        db.add(existing)
        db.commit()
        existing_id = existing.id
        account_id = account.id

    with SessionLocal() as db:
        losing_insert = PermissionSnapshot(
            account_id=account_id,
            external_source="authentik",
            external_user_id="snapshot-conflict-user-1",
            app_key="enterprise-blank",
        )
        row = _commit_snapshot_row(
            db,
            losing_insert,
            external_source="authentik",
            external_user_id="snapshot-conflict-user-1",
            app_key="enterprise-blank",
            values={
                "account_id": account_id,
                "groups": [],
                "grants": [],
                "grant_version": 2,
                "catalog_version": 2,
                "snapshot_version": "new",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
        assert row.id == existing_id
        assert row.snapshot_version == "new"


def test_late_older_snapshot_cannot_restore_revoked_grants() -> None:
    from blank_app.authz_api import _commit_snapshot_row

    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    with SessionLocal() as db:
        account = Account(
            username="snapshot-monotonic-user",
            email=None,
            password_hash=None,
            external_source="authentik",
            external_user_id="snapshot-monotonic-user-1",
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(account)
        db.flush()
        current = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id=account.external_user_id,
            app_key="enterprise-blank",
        )
        current = _commit_snapshot_row(
            db,
            current,
            external_source="authentik",
            external_user_id="snapshot-monotonic-user-1",
            app_key="enterprise-blank",
            values={
                "account_id": account.id,
                "groups": [],
                "grants": [],
                "grant_version": 5,
                "catalog_version": 3,
                "snapshot_version": "revoked-v5",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
        stale = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id="snapshot-monotonic-user-1",
            app_key="enterprise-blank",
        )
        persisted = _commit_snapshot_row(
            db,
            stale,
            external_source="authentik",
            external_user_id="snapshot-monotonic-user-1",
            app_key="enterprise-blank",
            values={
                "account_id": account.id,
                "groups": [],
                "grants": [
                    {
                        "permission": "authz.integration.manage",
                        "scope": "ALL",
                        "source_type": "direct",
                        "source_key": "",
                    }
                ],
                "grant_version": 4,
                "catalog_version": 3,
                "snapshot_version": "late-stale-v4",
                "fetched_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )

        assert persisted.id == current.id
        assert persisted.grant_version == 5
        assert persisted.snapshot_version == "revoked-v5"
        assert persisted.grants == []


def test_blank_descriptor_endpoint_enforces_active_keys_and_matches_manifest(monkeypatch) -> None:
    from blank_app.main import app

    with SessionLocal() as db:
        db.query(DescriptorKey).delete()
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {"app_key": "descriptor-current-app"}
        db.add(setting)
        db.commit()

    monkeypatch.setenv("BLANK_RUNTIME_ENV", "test")
    with TestClient(app) as client:
        headers = _admin_headers(client)
        created = client.post(
            "/api/v1/authz-integration/descriptor-keys",
            headers=headers,
            json={"name": "EasyAuth pull"},
        )
        token = created.json()["token"]
        key_id = created.json()["key"]["id"]

        assert client.get("/.well-known/easyauth-app.json").status_code == 401
        assert (
            client.get(
                "/.well-known/easyauth-app.json",
                headers={"Authorization": "Bearer invalid-key"},
            ).status_code
            == 401
        )
        pulled = client.get(
            "/.well-known/easyauth-app.json",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert pulled.status_code == 200, pulled.text
        descriptor = pulled.json()
        assert descriptor["descriptor_version"] == 1
        assert descriptor["app"]["app_key"] == "descriptor-current-app"
        assert descriptor["manifest"]["app"]["app_key"] == "descriptor-current-app"
        assert descriptor["manifest"]["permissions"]
        with SessionLocal() as db:
            assert db.get(DescriptorKey, uuid.UUID(key_id)).last_used_at is not None

        assert (
            client.patch(
                f"/api/v1/authz-integration/descriptor-keys/{key_id}",
                headers=headers,
                json={"active": False},
            ).status_code
            == 200
        )
        monkeypatch.setenv("BLANK_RUNTIME_ENV", "production")
        assert (
            client.get(
                "/.well-known/easyauth-app.json",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 401
        )
        with SessionLocal() as db:
            db.query(DescriptorKey).delete()
            db.commit()
        assert client.get("/.well-known/easyauth-app.json").status_code == 401
        monkeypatch.setenv("BLANK_RUNTIME_ENV", "test")
        assert client.get("/.well-known/easyauth-app.json").status_code == 200


def test_blank_shared_identity_connection_test_audits_success_and_failure(monkeypatch) -> None:
    from blank_app.adapters import BlankIntegrationAdapter
    from blank_app.main import app

    results = iter(
        (
            ConnectionTestResult(ok=True, latency_ms=7),
            ConnectionTestResult(ok=False, latency_ms=11, error_kind="unreachable", error_detail="secret detail"),
        )
    )
    monkeypatch.setattr(BlankIntegrationAdapter, "test_oidc", lambda _self: next(results))

    with TestClient(app) as client:
        headers = _admin_headers(client)
        assert client.post("/api/v1/identity-integration/connection-test", headers=headers).json()["ok"] is True
        assert client.post("/api/v1/identity-integration/connection-test", headers=headers).json()["ok"] is False

    with SessionLocal() as db:
        rows = (
            db.query(PlatformAuditLog)
            .filter(PlatformAuditLog.action == "identity.connection_test")
            .order_by(PlatformAuditLog.created_at.desc())
            .limit(2)
            .all()
        )
        assert {row.after_data["ok"] for row in rows} == {True, False}
        assert all(set(row.after_data) == {"ok", "latencyMs", "errorKind"} for row in rows)
        assert "secret detail" not in str([row.after_data for row in rows])


def test_blank_identity_settings_operations_use_locked_server_base_url(monkeypatch) -> None:
    import blank_app.adapters as adapters
    from blank_app.main import app
    from enterprise_platform.trusted_http import create_trusted_authority_transport

    with SessionLocal() as db:
        db.query(PlatformSetting).filter(PlatformSetting.key == "oidc").delete()
        db.commit()

    seen: list[str] = []
    discovery_redirect = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal discovery_redirect
        seen.append(str(request.url))
        if request.url.path == "/jwks-cross":
            return httpx.Response(302, headers={"location": "https://evil.example/jwks"})
        if request.url.path == "/.well-known/openid-configuration":
            if discovery_redirect:
                return httpx.Response(302, headers={"location": "https://evil.example/discovery"})
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.com/",
                    "authorization_endpoint": "https://identity.example.com/authorize",
                    "token_endpoint": "https://identity.example.com/token",
                    "jwks_uri": "https://identity.example.com/jwks",
                    "userinfo_endpoint": "https://identity.example.com/userinfo",
                },
            )
        return httpx.Response(200, json={"keys": [_valid_rsa_jwk()]})

    monkeypatch.setattr(
        adapters,
        "create_trusted_authority_transport",
        lambda base_url: create_trusted_authority_transport(base_url, transport=httpx.MockTransport(handler)),
    )
    settings_payload = {
        "enabled": True,
        "issuer": "https://identity.example.com/",
        "authorizationEndpoint": "https://identity.example.com/authorize",
        "tokenEndpoint": "https://identity.example.com/token",
        "jwksUri": "https://identity.example.com/jwks",
        "userinfoEndpoint": "https://identity.example.com/userinfo",
        "clientId": "enterprise-blank",
        "clientSecret": "oidc-client-secret",
        "scopes": "openid profile email",
        "redirectBaseUrl": "http://localhost:8001",
        "frontendBaseUrl": "http://localhost:3001",
        "serverBaseUrl": "http://host.docker.internal:9000",
    }
    with TestClient(app) as client:
        headers = _admin_headers(client)
        saved = client.put("/api/v1/identity-integration/settings", headers=headers, json=settings_payload)
        assert saved.status_code == 200
        assert saved.json()["serverBaseUrl"] == "http://host.docker.internal:9000"

        for invalid_server_base_url in (
            "/relative",
            "ftp://host.docker.internal:9000",
            "http://user:password@host.docker.internal:9000",
            "http://host.docker.internal:9000/#fragment",
        ):
            invalid_payload = {**settings_payload, "serverBaseUrl": invalid_server_base_url}
            assert (
                client.put("/api/v1/identity-integration/settings", headers=headers, json=invalid_payload).status_code
                == 422
            )

        connection = client.post("/api/v1/identity-integration/connection-test", headers=headers)
        assert connection.status_code == 200
        assert connection.json()["ok"] is True
        discovery = client.post(
            "/api/v1/identity-integration/discover",
            headers=headers,
            json={"issuer": "https://identity.example.com"},
        )
        assert discovery.status_code == 200
        assert discovery.json()["ok"] is True
        assert seen[:2] == [
            "http://host.docker.internal:9000/jwks",
            "http://host.docker.internal:9000/.well-known/openid-configuration",
        ]

        settings_payload["jwksUri"] = "https://identity.example.com/jwks-cross"
        assert (
            client.put("/api/v1/identity-integration/settings", headers=headers, json=settings_payload).status_code
            == 200
        )
        blocked_connection = client.post("/api/v1/identity-integration/connection-test", headers=headers)
        assert blocked_connection.json()["ok"] is False
        assert blocked_connection.json()["errorKind"] == "blocked"

        discovery_redirect = True
        blocked_discovery = client.post(
            "/api/v1/identity-integration/discover",
            headers=headers,
            json={"issuer": "https://identity.example.com"},
        )
        assert blocked_discovery.json()["ok"] is False
        assert blocked_discovery.json()["errorKind"] == "blocked"
        assert all("evil.example" not in url for url in seen)


def test_blank_security_headers_and_sensitive_no_store() -> None:
    from blank_app.main import SECURITY_HEADERS, app

    with TestClient(app) as client:
        regular = client.get("/__security_headers_probe__")
        sensitive = client.get("/api/v1/auth/oidc/status")
    for name, value in SECURITY_HEADERS.items():
        assert regular.headers[name] == value
        assert sensitive.headers[name] == value
    assert "Cache-Control" not in regular.headers
    assert sensitive.headers["Cache-Control"] == "no-store"


def test_trusted_principal_uses_cached_grants_and_rejects_bad_signature(monkeypatch) -> None:
    from blank_app.main import app

    # 该测试独立准备授权快照，避免依赖测试执行顺序。
    with SessionLocal() as db:
        account = db.query(Account).filter(Account.external_user_id == "authentik-user-1").one_or_none()
        if account is None:
            account = Account(
                username="trusted-principal-user",
                email="external@example.com",
                password_hash=None,
                external_source="authentik",
                external_user_id="authentik-user-1",
                active=True,
                is_admin=False,
                must_change_password=False,
            )
            db.add(account)
            db.flush()
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {
            "configured": True,
            "base_url": "https://auth.example.com",
            "app_key": "enterprise-blank",
            "auth_mode": "static_app_token",
            "has_credential": True,
            "credential": "opaque-test-token",
        }
        db.add(setting)
        snapshot = (
            db.query(PermissionSnapshot)
            .filter(
                PermissionSnapshot.external_source == "authentik",
                PermissionSnapshot.external_user_id == "authentik-user-1",
                PermissionSnapshot.app_key == "enterprise-blank",
            )
            .one_or_none()
        )
        if snapshot is None:
            snapshot = PermissionSnapshot(
                account_id=account.id,
                external_source="authentik",
                external_user_id="authentik-user-1",
                app_key="enterprise-blank",
            )
        snapshot.groups = []
        snapshot.grants = [
            {
                "permission": "authz.integration.view",
                "scope": "ALL",
                "source_type": "direct",
                "source_key": "",
            }
        ]
        snapshot.grant_version = 1
        snapshot.catalog_version = 1
        snapshot.snapshot_version = "1.1"
        snapshot.fetched_at = datetime.now(UTC)
        snapshot.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        db.add(snapshot)
        db.commit()

    monkeypatch.setenv("BLANK_PRINCIPAL_MODE", "jwt")
    monkeypatch.setenv("BLANK_PRINCIPAL_HEADER", "X-Enterprise-Principal-JWT")
    monkeypatch.setenv("BLANK_PRINCIPAL_ISSUER", "https://identity.example.com/")
    monkeypatch.setenv("BLANK_PRINCIPAL_AUDIENCE", "enterprise-blank")
    monkeypatch.setenv("BLANK_PRINCIPAL_SECRET", "principal-test-secret-at-least-32-bytes")
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "authentik-user-1",
            "iss": "https://identity.example.com/",
            "aud": "enterprise-blank",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "active": True,
            "name": "External User",
            "email": "external@example.com",
        },
        "principal-test-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me", headers={"X-Enterprise-Principal-JWT": token})
        assert me.status_code == 200
        assert me.json()["permissions"] == ["authz.integration.view"]
        assert client.get("/api/v1/auth/me", headers={"X-Enterprise-Principal-JWT": "bad"}).status_code == 401


SECURITY_WRITE_ENDPOINTS = (
    ("POST", "/api/v1/users/me/totp/begin", None),
    ("POST", "/api/v1/users/me/totp/confirm", {"code": "123456"}),
    ("POST", "/api/v1/users/me/totp/disable", {"password": "irrelevant", "code": "123456"}),
    ("GET", "/api/v1/users/me/totp/status", None),
    ("GET", "/api/v1/users/me/passkeys", None),
    ("POST", "/api/v1/users/me/passkeys/register/begin", None),
    ("POST", "/api/v1/users/me/passkeys/register/complete", {"stateToken": "x", "credential": {}, "name": "k"}),
    ("POST", "/api/v1/users/me/password", {"currentPassword": "a-very-long-one", "newPassword": "another-long-1"}),
)


def test_external_principal_cannot_manage_local_credentials(monkeypatch) -> None:
    """外部身份即使拿到 auth.* grant 也不得写本地 TOTP/Passkey 凭据,且 DB 不变。"""

    from blank_app.main import app

    external_id = f"security-capability-{uuid.uuid4().hex}"
    grants = [
        {"permission": code, "scope": "SELF", "source_type": "direct", "source_key": ""}
        for code in ("auth.totp.create", "auth.totp.advance", "auth.passkey.view", "auth.passkey.create")
    ]
    with SessionLocal() as db:
        setting = db.get(PlatformSetting, "easyauth") or PlatformSetting(key="easyauth")
        setting.value = {**dict(setting.value or {}), "app_key": "enterprise-blank", "configured": True}
        db.add(setting)
        account = Account(
            username=f"external-security-{uuid.uuid4().hex[:8]}",
            email="external-security@example.com",
            password_hash=None,
            external_source="authentik",
            external_user_id=external_id,
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add(account)
        db.flush()
        db.add(
            PermissionSnapshot(
                account_id=account.id,
                external_source="authentik",
                external_user_id=external_id,
                app_key="enterprise-blank",
                groups=[],
                grants=grants,
                grant_version=1,
                catalog_version=1,
                snapshot_version="1.1",
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        db.commit()
        account_id = account.id

    monkeypatch.setenv("BLANK_PRINCIPAL_MODE", "jwt")
    monkeypatch.setenv("BLANK_PRINCIPAL_HEADER", "X-Enterprise-Principal-JWT")
    monkeypatch.setenv("BLANK_PRINCIPAL_ISSUER", "https://identity.example.com/")
    monkeypatch.setenv("BLANK_PRINCIPAL_AUDIENCE", "enterprise-blank")
    monkeypatch.setenv("BLANK_PRINCIPAL_SECRET", "principal-test-secret-at-least-32-bytes")
    now = datetime.now(UTC)
    principal = jwt.encode(
        {
            "sub": external_id,
            "iss": "https://identity.example.com/",
            "aud": "enterprise-blank",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "active": True,
            "name": "External Security User",
            "email": "external-security@example.com",
        },
        "principal-test-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    headers = {"X-Enterprise-Principal-JWT": principal}

    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        capabilities = me.json()["securityCapabilities"]
        # 声明的 capability 必须全为 false,并与后端实际放行一致。
        assert not any(capabilities.values())
        assert "auth.totp.create" in me.json()["permissions"]

        for method, path, body in SECURITY_WRITE_ENDPOINTS:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
        assert client.delete(f"/api/v1/users/me/passkeys/{uuid.uuid4()}", headers=headers).status_code == 403

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account.totp_enabled is False
        assert account.totp_secret is None
        assert account.totp_pending_secret is None
        assert account.password_hash is None
        assert db.query(Passkey).filter(Passkey.account_id == account_id).count() == 0


def test_local_superuser_keeps_security_capabilities_consistent_with_enforcement() -> None:
    """本地超管的 capability 声明为 true,同一批端点必须真实可用(非 403)。"""

    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        capabilities = client.get("/api/v1/auth/me", headers=headers).json()["securityCapabilities"]
        assert all(capabilities.values())
        for method, path, body in SECURITY_WRITE_ENDPOINTS:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code != 403, f"{method} {path} -> {response.text}"


def test_blank_role_groups_require_current_app_key_and_unexpired_snapshot() -> None:
    from blank_app.adapters import _snapshot_role_groups

    now = datetime.now(UTC)
    current_app = f"current-{uuid.uuid4().hex}"
    other_app = f"other-{uuid.uuid4().hex}"
    external_id = f"role-groups-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        setting = db.get(PlatformSetting, "easyauth")
        setting_existed = setting is not None
        setting = setting or PlatformSetting(key="easyauth")
        previous_setting = dict(setting.value or {})
        setting.value = {**previous_setting, "app_key": current_app}
        account = Account(
            username=f"role-groups-{uuid.uuid4().hex}",
            email=None,
            password_hash=None,
            external_source="authentik",
            external_user_id=external_id,
            active=True,
            is_admin=False,
            must_change_password=False,
        )
        db.add_all([setting, account])
        db.flush()
        account_id = account.id
        current = PermissionSnapshot(
            account_id=account.id,
            external_source="authentik",
            external_user_id=external_id,
            app_key=current_app,
            groups=[{"key": "current", "kind": "role", "name": "Current Role"}],
            grants=[],
            grant_version=1,
            catalog_version=1,
            snapshot_version="current",
            fetched_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        db.add_all(
            [
                current,
                PermissionSnapshot(
                    account_id=account.id,
                    external_source="authentik",
                    external_user_id=external_id,
                    app_key=other_app,
                    groups=[{"key": "other", "kind": "role", "name": "Other Role"}],
                    grants=[],
                    grant_version=1,
                    catalog_version=1,
                    snapshot_version="other",
                    fetched_at=now + timedelta(minutes=1),
                    expires_at=now + timedelta(minutes=10),
                ),
            ]
        )
        db.commit()
        db.expunge(account)
    try:
        assert _snapshot_role_groups(account) == ["Current Role"]
        with SessionLocal() as db:
            row = (
                db.query(PermissionSnapshot)
                .filter(PermissionSnapshot.account_id == account_id, PermissionSnapshot.app_key == current_app)
                .one()
            )
            row.expires_at = now - timedelta(seconds=1)
            db.commit()
        assert _snapshot_role_groups(account) == []
    finally:
        with SessionLocal() as db:
            db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account_id).delete()
            db.query(Account).filter(Account.id == account_id).delete()
            setting = db.get(PlatformSetting, "easyauth")
            if setting_existed:
                setting.value = previous_setting
            elif setting is not None:
                db.delete(setting)
            db.commit()


def test_shared_oidc_status_authorize_safe_next_and_callback(monkeypatch) -> None:
    import enterprise_platform.oidc as oidc
    from blank_app.main import app

    with SessionLocal() as db:
        row = db.get(PlatformSetting, "oidc") or PlatformSetting(key="oidc")
        row.value = {}
        db.add(row)
        db.commit()

    with TestClient(app) as client:
        assert client.get("/api/v1/auth/oidc/status").json()["enabled"] is False
        disabled = client.get("/api/v1/auth/oidc/authorize", follow_redirects=False)
        assert disabled.status_code == 404

        with SessionLocal() as db:
            row = db.get(PlatformSetting, "oidc")
            row.value = {
                "enabled": True,
                "issuer": "https://identity.example.com/",
                "authorization_endpoint": "https://identity.example.com/authorize",
                "token_endpoint": "https://identity.example.com/token",
                "jwks_uri": "https://identity.example.com/jwks",
                "userinfo_endpoint": "",
                "client_id": "enterprise-blank",
                "client_secret": encrypt_secret("oidc-client-secret"),
                "scopes": "openid profile email",
                "redirect_uri": "http://localhost:8001/api/v1/auth/oidc/callback",
                "frontend_base_url": "http://localhost:3001",
            }
            db.commit()

        authorize = client.get(
            "/api/v1/auth/oidc/authorize?next=https://evil.example.com",
            follow_redirects=False,
        )
        assert authorize.status_code == 302
        query = parse_qs(urlparse(authorize.headers["location"]).query)
        state = query["state"][0]
        cookie = client.cookies.get(oidc.STATE_COOKIE_NAME)
        claims = jwt.decode(cookie, oidc_state_key(), algorithms=["HS256"])
        assert claims["next"] == ""

        authorize = client.get("/api/v1/auth/oidc/authorize?next=/admin", follow_redirects=False)
        query = parse_qs(urlparse(authorize.headers["location"]).query)
        state = query["state"][0]
        cookie = client.cookies.get(oidc.STATE_COOKIE_NAME)
        claims = jwt.decode(cookie, oidc_state_key(), algorithms=["HS256"])
        assert claims["next"] == "/admin"

        denied = client.get(
            f"/api/v1/auth/oidc/callback?error=access_denied&error_description=nope&state={state}",
            follow_redirects=False,
        )
        assert denied.status_code == 302
        assert "oidc_error=access_denied" in denied.headers["location"]
        assert "oidc_error_detail" not in denied.headers["location"]
        assert "nope" not in denied.headers["location"]

        # error 回调会清理 state cookie，重新开始一次合法授权用于 happy path。
        authorize = client.get("/api/v1/auth/oidc/authorize?next=/admin", follow_redirects=False)
        state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]

        monkeypatch.setattr(
            oidc,
            "exchange_code",
            lambda config, code, verifier: {"id_token": "id", "access_token": ""},
        )
        monkeypatch.setattr(
            oidc,
            "validate_id_token",
            lambda config, token, nonce: {
                "sub": "oidc-user-1",
                "name": "OIDC User",
                "email": "oidc@example.com",
                "picture": "https://example.com/avatar.png",
            },
        )
        callback = client.get(
            f"/api/v1/auth/oidc/callback?code=ok&state={state}",
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert callback.headers["location"].startswith("http://localhost:3001/zh-CN/login/oidc-complete#")
        assert "next=%2Fadmin" in callback.headers["location"]


def test_blank_oidc_server_base_url_rewrites_private_hops_and_locks_redirects(monkeypatch) -> None:
    import blank_app.oidc_adapter as oidc_adapter
    from enterprise_platform.trusted_http import create_trusted_authority_transport

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/token":
            return httpx.Response(302, headers={"location": "https://evil.example/token"})
        return httpx.Response(200, json={"keys": []})

    monkeypatch.setattr(
        oidc_adapter,
        "create_trusted_authority_transport",
        lambda base_url: create_trusted_authority_transport(base_url, transport=httpx.MockTransport(handler)),
    )
    with SessionLocal() as db:
        row = db.get(PlatformSetting, "oidc") or PlatformSetting(key="oidc")
        previous = dict(row.value or {})
        row.value = {
            "enabled": True,
            "issuer": "https://identity.example.com/application/o/blank/",
            "authorization_endpoint": "https://identity.example.com/authorize",
            "token_endpoint": "https://identity.example.com/token",
            "jwks_uri": "https://identity.example.com/jwks",
            "userinfo_endpoint": "https://identity.example.com/userinfo?source=oidc",
            "client_id": "enterprise-blank",
            "client_secret": encrypt_secret("oidc-client-secret"),
            "redirect_uri": "http://localhost:8001/api/v1/auth/oidc/callback",
            "frontend_base_url": "http://localhost:3001",
            "server_base_url": "http://host.docker.internal:9000",
        }
        db.add(row)
        db.commit()
    try:
        config = oidc_adapter.BlankOidcHost().config()
        assert config.authorization_endpoint == "https://identity.example.com/authorize"
        assert config.token_endpoint == "http://host.docker.internal:9000/token"
        assert config.jwks_uri == "http://host.docker.internal:9000/jwks"
        assert config.userinfo_endpoint == "http://host.docker.internal:9000/userinfo?source=oidc"
        assert config.http_transport("GET", config.jwks_uri).status_code == 200
        with pytest.raises(UnsafeOutboundUrlError):
            config.http_transport("POST", config.token_endpoint)
        assert seen == [
            "http://host.docker.internal:9000/jwks",
            "http://host.docker.internal:9000/token",
        ]
    finally:
        with SessionLocal() as db:
            row = db.get(PlatformSetting, "oidc")
            row.value = previous
            db.commit()
