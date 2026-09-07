"""第三方最小宿主与共享授权/JWKS/OIDC 合同。"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform.authorization import create_authorization_operations_router
from enterprise_platform.jwks import probe_jwks
from enterprise_platform.oidc import OidcConfig, OidcRouteConfig, create_oidc_router
from enterprise_platform.safe_http import UnsafeOutboundUrlError
from enterprise_platform.schemas import (
    AuthorizationCatalogItem,
    AuthorizationConnectionResult,
    AuthorizationCountStats,
    AuthorizationEasyAuthSummary,
    AuthorizationPrincipalSummary,
    AuthorizationSettings,
    AuthorizationSnapshot,
    AuthorizationSnapshotStats,
    AuthorizationStatus,
    CurrentUser,
    DescriptorKeyCreateResponse,
    DescriptorKeyResponse,
    MyGrantResponse,
)
from enterprise_platform.trusted_http import create_trusted_authority_transport


class MinimalAuthorizationHost:
    def __init__(self) -> None:
        self.key_id = uuid.uuid4()
        self.snapshot_query: tuple[int, int, str] | None = None
        self.snapshot = AuthorizationSnapshot(
            user_id=uuid.uuid4(),
            display_name="Minimal User",
            external_user_id="minimal-1",
            app_key="minimal",
            grant_count=1,
            grant_version=1,
            catalog_version=1,
            snapshot_version="v1",
            role_groups=["Operators"],
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            expired=False,
        )

    def status(self):
        return AuthorizationStatus(
            easyauth=AuthorizationEasyAuthSummary(
                configured=True,
                base_url="https://easyauth.example.test",
                app_key="minimal",
                auth_mode="static_app_token",
                has_credential=True,
            ),
            principal=AuthorizationPrincipalSummary(
                mode="jwt",
                header_name="X-Enterprise-Principal",
                issuer="https://identity.example.test",
                audience="minimal",
            ),
            catalog=AuthorizationCountStats(active_count=1, total_count=1),
            snapshots=AuthorizationSnapshotStats(total=1),
        )

    def connection_test(self, *, actor_id):
        return AuthorizationConnectionResult(ok=True, snapshot_version="v1", grant_count=1)

    def get_settings(self):
        return AuthorizationSettings(
            configured=True,
            base_url="https://easyauth.example.test",
            app_key="minimal",
            auth_mode="static_app_token",
            has_credential=True,
            permission_request_url="https://access.example.test",
        )

    def save_settings(self, payload, *, actor_id):
        return AuthorizationSettings(app_key="minimal", permission_request_url=payload.permission_request_url)

    def list_catalog(self, *, search, active):
        return [
            AuthorizationCatalogItem(
                code="sample.view",
                name_zh="查看示例",
                name_en="View sample",
                domain="sample",
                resource="sample",
                action="view",
                supported_scopes=["ALL"],
                risk_level="standard",
                active=True,
            )
        ]

    def list_snapshots(self, *, search, offset, limit, sort):
        self.snapshot_query = (offset, limit, sort)
        return ([self.snapshot] if offset == 0 else [], 1)

    def refresh_snapshot(self, user_id, *, actor_id):
        return self.snapshot

    def manifest(self, *, schema_version, actor_id):
        return {"schema_version": schema_version or 1, "app": {"app_key": "minimal"}, "permissions": []}

    def my_grants(self, *, actor_id):
        return [MyGrantResponse(permission="sample.view", data_scope="ALL")]

    def list_descriptor_keys(self):
        return [self._key()]

    def create_descriptor_key(self, payload, *, actor_id):
        return DescriptorKeyCreateResponse(key=self._key(), token="epd_once")

    def update_descriptor_key(self, key_id, payload, *, actor_id):
        return self._key(active=payload.active)

    def delete_descriptor_key(self, key_id, *, actor_id):
        return None

    def _key(self, active=True):
        return DescriptorKeyResponse(
            id=self.key_id,
            name="sync",
            token_prefix="epd_",
            active=active,
            created_at=datetime.now(UTC),
        )


def test_minimal_third_party_host_exposes_complete_shared_ui_contract() -> None:
    host = MinimalAuthorizationHost()
    app = FastAPI()
    app.include_router(
        create_authorization_operations_router(
            host,
            current_user_dependency=lambda: CurrentUser(
                id="actor",
                name="Actor",
                permissions=["authz.integration.view", "authz.integration.manage"],
            ),
            permission_dependency_factory=lambda _code: lambda: None,
        ),
        prefix="/api/v7",
    )
    client = TestClient(app)
    paths = (
        "/status",
        "/settings",
        "/permission-catalog",
        "/snapshots?limit=1&offset=0&sort=fetched_at_desc",
        "/manifest",
        "/my-grants",
        "/descriptor-keys",
    )
    for path in paths:
        assert client.get(f"/api/v7/authz-integration{path}").status_code == 200
    status = client.get("/api/v7/authz-integration/status").json()
    assert status["easyauth"]["baseUrl"] == "https://easyauth.example.test"
    assert status["easyauth"]["appKey"] == "minimal"
    assert client.get("/api/v7/authz-integration/permission-catalog").json()[0]["code"] == "sample.view"
    snapshots = client.get("/api/v7/authz-integration/snapshots?limit=1")
    assert snapshots.headers["X-Total-Count"] == "1"
    assert snapshots.json()[0]["roleGroups"] == ["Operators"]
    default_page = client.get("/api/v7/authz-integration/snapshots")
    assert default_page.status_code == 200
    assert host.snapshot_query == (0, 50, "fetched_at_desc")
    assert client.get("/api/v7/authz-integration/snapshots?limit=200").status_code == 200
    assert client.get("/api/v7/authz-integration/snapshots?limit=201").status_code == 422
    assert client.get("/api/v7/authz-integration/snapshots?sort=drop_table").status_code == 400
    assert client.post("/api/v7/authz-integration/connection-test").json()["snapshotVersion"] == "v1"
    assert client.post("/api/v7/authz-integration/descriptor-keys", json={"name": "sync"}).json()["token"] == "epd_once"
    assert (
        client.patch(f"/api/v7/authz-integration/descriptor-keys/{host.key_id}", json={"active": False}).json()[
            "active"
        ]
        is False
    )
    assert client.delete(f"/api/v7/authz-integration/descriptor-keys/{host.key_id}").status_code == 204


def test_shared_authorization_viewer_receives_only_coarse_business_status() -> None:
    host = MinimalAuthorizationHost()
    app = FastAPI()
    app.include_router(
        create_authorization_operations_router(
            host,
            current_user_dependency=lambda: CurrentUser(
                id="viewer",
                name="Viewer",
                permissions=["authz.integration.view"],
            ),
            permission_dependency_factory=lambda _code: lambda: None,
        ),
        prefix="/api/v7",
    )
    client = TestClient(app)

    status = client.get("/api/v7/authz-integration/status")
    assert status.status_code == 200
    assert status.json() == {
        "easyauth": {"configured": True, "hasCredential": True},
        "principal": {"configured": True},
        "catalog": {"configured": True},
        "snapshots": {"configured": True},
    }
    for forbidden in ("appKey", "baseUrl", "authMode", "headerName", "issuer", "audience"):
        assert forbidden not in status.text

    settings = client.get("/api/v7/authz-integration/settings")
    assert settings.json() == {
        "configured": True,
        "hasCredential": True,
        "permissionRequestUrl": "https://access.example.test",
    }
    assert "appKey" not in settings.text
    assert "baseUrl" not in settings.text

    catalog = client.get("/api/v7/authz-integration/permission-catalog").json()
    assert catalog == [{"nameZh": "查看示例", "nameEn": "View sample", "active": True}]
    assert "sample.view" not in str(catalog)
    assert "supportedScopes" not in str(catalog)

    snapshots = client.get("/api/v7/authz-integration/snapshots").json()
    assert snapshots[0]["displayName"] == "Minimal User"
    for forbidden in ("externalUserId", "appKey", "grantVersion", "catalogVersion", "snapshotVersion"):
        assert forbidden not in snapshots[0]

    grants = client.get("/api/v7/authz-integration/my-grants").json()
    assert grants == [{"active": True}]
    assert "permission" not in str(grants)
    assert "dataScope" not in str(grants)


def test_strict_jwks_probe_rejects_404_non_json_and_empty_keys() -> None:
    responses = [
        httpx.Response(404),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"keys": []}),
        httpx.Response(200, json={"keys": [_rsa_jwk()]}),
    ]

    def transport(_method, _url, **_kwargs):
        return responses.pop(0)

    results = [probe_jwks("https://id.example/jwks", transport=transport) for _ in range(4)]
    assert [result.ok for result in results] == [False, False, False, True]
    assert [result.error_kind for result in results[:3]] == ["http_error", "invalid_response", "invalid_response"]


def test_strict_jwks_validation_rejects_non_verification_keys() -> None:
    invalid = [
        {**_rsa_jwk(), "n": "AQ"},
        {**_rsa_jwk(), "e": "Ag"},
        {**_rsa_jwk(), "use": "enc"},
        {**_rsa_jwk(), "alg": "RS512"},
        {"kid": "ec", "kty": "EC", "crv": "P-384", "x": "AQ", "y": "AQ", "use": "sig"},
    ]
    for key in invalid:
        result = probe_jwks(
            "https://id.example/jwks",
            transport=lambda *_args, key=key, **_kwargs: httpx.Response(200, json={"keys": [key]}),
        )
        assert result.ok is False
        assert result.error_kind == "invalid_response"


def _rsa_jwk() -> dict[str, str]:
    modulus = bytes([0x80]) + bytes(range(1, 256))
    return {
        "kid": "one",
        "kty": "RSA",
        "n": base64.urlsafe_b64encode(modulus).rstrip(b"=").decode(),
        "e": "AQAB",
        "use": "sig",
        "alg": "RS256",
    }


def test_trusted_authority_transport_allows_private_dns_but_locks_redirect_authority() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/jwks"})
        if request.url.path == "/cross":
            return httpx.Response(302, headers={"location": "https://evil.example/jwks"})
        return httpx.Response(200, json={"keys": [_rsa_jwk()]})

    request = create_trusted_authority_transport(
        "http://host.docker.internal:9000",
        transport=httpx.MockTransport(handler),
    )
    assert request("GET", "http://host.docker.internal:9000/start").status_code == 200
    assert seen == ["http://host.docker.internal:9000/start", "http://host.docker.internal:9000/jwks"]
    with pytest.raises(UnsafeOutboundUrlError):
        request("GET", "http://127.0.0.1:9000/jwks")
    with pytest.raises(UnsafeOutboundUrlError):
        request("GET", "http://host.docker.internal:9000/cross")


class MinimalOidcHost:
    def __init__(self):
        self.revoked_subjects: list[str] = []

    def revoke_sessions_by_subject(self, sub: str) -> int:
        self.revoked_subjects.append(sub)
        return 0

    def config(self):
        return OidcConfig(
            enabled=True,
            issuer="https://id.example/application/o/app/",
            authorization_endpoint="https://id.example/authorize",
            token_endpoint="https://id.example/token",
            jwks_uri="https://id.example/jwks",
            userinfo_endpoint="",
            client_id="minimal",
            client_secret="secret",
            scopes="openid",
            redirect_uri="https://api.example/gateway/v2/auth/oidc/callback",
            frontend_base_url="https://app.example",
            signing_secret="x" * 32,
        )

    def upsert_identity(self, identity):
        return "actor"

    def issue_session(self, account_id):
        return "token"


def test_oidc_factory_uses_host_paths_and_localizes_callback_errors() -> None:
    app = FastAPI()
    routes = OidcRouteConfig(api_base_path="/gateway/v2", default_locale="en")
    app.include_router(create_oidc_router(MinimalOidcHost(), routes=routes), prefix="/gateway/v2")
    client = TestClient(app)
    assert client.get("/gateway/v2/auth/oidc/status").json()["authorizePath"] == "/gateway/v2/auth/oidc/authorize"
    authorize = client.get("/gateway/v2/auth/oidc/authorize?next=/zh-CN/settings", follow_redirects=False)
    assert authorize.status_code == 302
    assert "Path=/gateway/v2/auth/oidc" in authorize.headers["set-cookie"]
    state = httpx.URL(authorize.headers["location"]).params["state"]
    denied = client.get(
        "/gateway/v2/auth/oidc/callback",
        params={"error": "access_denied", "error_description": "provider-secret", "state": state},
        follow_redirects=False,
    )
    denied_location = denied.headers["location"]
    assert denied_location.startswith("https://app.example/zh-CN/login?")
    assert httpx.URL(denied_location).params["oidc_error"] == "access_denied"
    assert "oidc_error_detail" not in httpx.URL(denied_location).params
    assert "provider-secret" not in denied_location

    client.cookies.set("NEXT_LOCALE", "en")
    authorize_en = client.get("/gateway/v2/auth/oidc/authorize", follow_redirects=False)
    state_en = httpx.URL(authorize_en.headers["location"]).params["state"]
    denied_en = client.get(
        f"/gateway/v2/auth/oidc/callback?error=access_denied&state={state_en}",
        follow_redirects=False,
    )
    assert denied_en.headers["location"].startswith("https://app.example/en/login?")

    unknown = client.get("/gateway/v2/auth/oidc/authorize", follow_redirects=False)
    unknown_state = httpx.URL(unknown.headers["location"]).params["state"]
    unknown_error = client.get(
        "/gateway/v2/auth/oidc/callback",
        params={"error": "attacker_controlled", "error_description": "do-not-reflect", "state": unknown_state},
        follow_redirects=False,
    )
    unknown_location = unknown_error.headers["location"]
    assert httpx.URL(unknown_location).params["oidc_error"] == "provider_error"
    assert "do-not-reflect" not in unknown_location

    client.get("/gateway/v2/auth/oidc/authorize", follow_redirects=False)
    mismatch = client.get(
        "/gateway/v2/auth/oidc/callback",
        params={"error": "access_denied", "error_description": "do-not-reflect", "state": "tampered"},
        follow_redirects=False,
    )
    mismatch_location = mismatch.headers["location"]
    assert httpx.URL(mismatch_location).params["oidc_error"] == "state_mismatch"
    assert "oidc_error_detail" not in httpx.URL(mismatch_location).params
    assert "access_denied" not in mismatch_location
    assert "do-not-reflect" not in mismatch_location
