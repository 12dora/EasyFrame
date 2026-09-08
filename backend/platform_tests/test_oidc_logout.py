"""后通道注销使用真实签名，仅替换外部 JWKS HTTP。"""

import time
import uuid
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwk, jwt

from blank_app.database import SessionLocal
from blank_app.models import Account
from blank_app.oidc_adapter import BlankOidcHost
from enterprise_platform import oidc
from enterprise_platform.auth import AuthError
from enterprise_platform.oidc_logout import LOGOUT_EVENT
from platform_tests.test_shared_authorization_contract import MinimalOidcHost


@pytest.fixture(params=["RS256", "ES256"])
def logout_setup(request):
    algorithm = request.param
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if algorithm == "RS256"
        else ec.generate_private_key(ec.SECP256R1())
    )
    public = {
        **jwk.construct(key.public_key(), algorithm).to_dict(),
        "kid": "logout-key",
        "alg": algorithm,
        "use": "sig",
    }
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"keys": [public]}))
    host = MinimalOidcHost()
    config = replace(host.config(), jwks_uri=f"https://id.example/{uuid.uuid4()}/jwks")
    with httpx.Client(transport=transport) as http:
        config = replace(config, http_transport=lambda method, url, **_kwargs: http.request(method, url))
        host.config = lambda: config
        app = FastAPI()
        app.include_router(oidc.create_oidc_router(host), prefix="/api/v1")
        with TestClient(app) as client:
            claims = {
                "iss": config.issuer,
                "aud": config.client_id,
                "iat": int(time.time()),
                "jti": str(uuid.uuid4()),
                "sub": "upstream-user",
                "events": {LOGOUT_EVENT: {}},
            }
            yield (
                client,
                host,
                claims,
                lambda payload: jwt.encode(payload, key, algorithm=algorithm, headers={"kid": "logout-key"}),
            )
    oidc._jwks_cache.pop(config.jwks_uri, None)


def test_logout_calls_minimal_host_and_accepts_repeated_token(logout_setup, monkeypatch):
    client, host, claims, sign = logout_setup
    logged: list[str] = []

    def capture_info(message, *args, **_kwargs):
        logged.append(message % args if args else str(message))

    monkeypatch.setattr("enterprise_platform.oidc_logout._LOG.info", capture_info)
    for _ in range(2):
        response = client.post("/api/v1/auth/oidc/backchannel-logout", data={"logout_token": sign(claims)})
        assert response.status_code == 200
        assert response.json() == {}
        assert response.headers["cache-control"] == "no-store"
    assert host.revoked_subjects == [claims["sub"], claims["sub"]]
    assert any("revoked_accounts=0" in line for line in logged)
    assert client.get("/api/v1/auth/oidc/backchannel-logout").status_code == 405


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "wrong"},
        {"iss": "wrong"},
        {"events": None},
        {"events": []},
        {"events": {}},
        {"nonce": None},
        {"sub": None, "sid": "session"},
        {"exp": 1},
        {"iat": 1},
        {"iat": None},
        {"jti": None},
        {"aud": None},
        {"iss": None},
        {"iat": "yesterday"},
        {"iat": []},
        {"jti": ""},
    ],
)
def test_invalid_logout_tokens_fail_closed(logout_setup, change):
    client, host, claims, sign = logout_setup
    claims.update(change)
    claims = {key: value for key, value in claims.items() if value is not None or key == "nonce"}
    response = client.post("/api/v1/auth/oidc/backchannel-logout", data={"logout_token": sign(claims)})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"
    assert host.revoked_subjects == []
    if "sid" in change:
        assert "sid-only logout is unsupported" in response.json()["error_description"]


def test_signature_missing_form_and_old_token_with_valid_exp(logout_setup):
    client, host, claims, sign = logout_setup
    token = sign(claims)
    head, payload, signature = token.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    for data in ({}, {"logout_token": "malformed"}, {"logout_token": f"{head}.{payload}.{signature}"}):
        assert client.post("/api/v1/auth/oidc/backchannel-logout", data=data).status_code == 400
    assert host.revoked_subjects == []
    claims.update(iat=1, exp=int(time.time()) + 600, aud=["other", host.config().client_id])
    assert client.post("/api/v1/auth/oidc/backchannel-logout", data={"logout_token": sign(claims)}).status_code == 200


@pytest.mark.parametrize("settings,status", [({"enabled": False}, 404), ({"client_id": ""}, 409)])
def test_logout_unconfigured(logout_setup, settings, status):
    client, host, _, _ = logout_setup
    config = replace(host.config(), **settings)
    host.config = lambda: config
    response = client.post("/api/v1/auth/oidc/backchannel-logout", data={"logout_token": "invalid"})
    assert response.status_code == status
    assert response.json()["kind"] == "not_configured"


def test_blank_logout_revokes_only_matching_external_account(logout_setup):
    from blank_app.adapters import account_adapter, request_token

    client, host, claims, sign = logout_setup
    sub = str(uuid.uuid4())
    with SessionLocal() as db:
        account = Account(username=f"logout-{sub}", external_source="authentik", external_user_id=sub, active=True)
        other = Account(username=f"other-{sub}", external_source="other", external_user_id=sub, active=True)
        db.add_all([account, other])
        db.commit()
        account_id, other_id = str(account.id), other.id
    session = account_adapter.issue_session(account_id)
    host.revoke_sessions_by_subject = BlankOidcHost().revoke_sessions_by_subject
    claims["sub"] = sub
    for _ in range(2):
        assert (
            client.post("/api/v1/auth/oidc/backchannel-logout", data={"logout_token": sign(claims)}).status_code == 200
        )
    with SessionLocal() as db:
        assert db.get(Account, uuid.UUID(account_id)).sessions_revoked_at is not None
        assert db.get(Account, other_id).sessions_revoked_at is None
    context = request_token.set(session)
    try:
        with pytest.raises(AuthError, match="登录态已失效"):
            account_adapter.current_user()
    finally:
        request_token.reset(context)
    assert BlankOidcHost().revoke_sessions_by_subject("unknown") == 0
