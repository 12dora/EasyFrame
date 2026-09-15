"""RP 发起注销：回调落库 id_token，end-session 返回 Authentik POST 表单字段。"""

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from enterprise_platform import oidc
from enterprise_platform.schemas import CurrentUser
from platform_tests.test_shared_authorization_contract import MinimalOidcHost

_HINT = "raw-id-token.with.dingtalk-claims"


def _actor() -> CurrentUser:
    return CurrentUser(id="actor", name="alice")


def _client(host=None, current_user=_actor):
    host = host or MinimalOidcHost()
    app = FastAPI()
    app.include_router(oidc.create_oidc_router(host, current_user_dependency=current_user), prefix="/api/v1")
    return TestClient(app), host


def _complete_login(client, monkeypatch, host, id_token=_HINT, *, silent=False):
    monkeypatch.setattr(oidc, "exchange_code", lambda *_: {"id_token": id_token, "access_token": ""})
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": "alice", "name": "Alice", "picture": "x"})
    path = "/api/v1/auth/oidc/authorize?silent=1" if silent else "/api/v1/auth/oidc/authorize"
    authorize = client.get(path, follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    response = client.get("/api/v1/auth/oidc/callback", params={"code": "ok", "state": state}, follow_redirects=False)
    assert response.status_code == 302
    assert host.stored_id_tokens["actor"] == id_token


def test_callback_stores_id_token(monkeypatch):
    host = MinimalOidcHost()
    client, _ = _client(host)
    _complete_login(client, monkeypatch, host)


def test_silent_callback_stores_id_token(monkeypatch):
    host = MinimalOidcHost()
    client, _ = _client(host)
    _complete_login(client, monkeypatch, host, silent=True)


def test_callback_skips_storing_oversized_id_token(monkeypatch):
    huge = "a" * (oidc.MAX_STORED_ID_TOKEN_BYTES + 1)
    host = MinimalOidcHost()
    client, _ = _client(host)
    monkeypatch.setattr(oidc, "exchange_code", lambda *_: {"id_token": huge, "access_token": ""})
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": "alice", "name": "Alice", "picture": "x"})
    authorize = client.get("/api/v1/auth/oidc/authorize", follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    response = client.get("/api/v1/auth/oidc/callback", params={"code": "ok", "state": state}, follow_redirects=False)
    assert response.status_code == 302
    assert host.stored_id_tokens == {}


def test_end_session_returns_url_and_fields(monkeypatch):
    host = MinimalOidcHost()
    client, _ = _client(host)
    _complete_login(client, monkeypatch, host)
    status = client.get("/api/v1/auth/oidc/status").json()
    assert status["endSessionUrl"] == "https://id.example/application/o/app/end-session/"
    response = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": "/zh-CN/login"})
    assert response.status_code == 200
    assert response.json() == {
        "url": status["endSessionUrl"],
        "method": "POST",
        "fields": {
            "id_token_hint": _HINT,
            "post_logout_redirect_uri": "https://app.example/zh-CN/login",
        },
    }
    omitted = client.post("/api/v1/auth/oidc/end-session", json={})
    assert omitted.status_code == 200
    assert omitted.json()["fields"] == {"id_token_hint": _HINT}


@pytest.mark.parametrize(
    "return_to",
    [
        "",
        "login",
        "//evil.example/login",
        "https://evil.example/login",
        "/foo\\bar",
        "/foo://bar",
        "/\nlogin",
        "/%2F%2Fevil.example/login",
        "/login?next=%2F%2Fevil",
        "/..",
        "/../login",
        "/zh-CN/../en/login",
        "/foo/./bar",
        "/zh-CN/login ",
        "/ zh-CN/login",
        "/\xa0login",
        "/" + "a" * 5000,
        "/zh-CN/登录",
    ],
)
def test_end_session_rejects_unsafe_return_to(return_to):
    host = MinimalOidcHost()
    host.stored_id_tokens["actor"] = _HINT
    client, _ = _client(host)
    response = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": return_to})
    assert response.status_code == 422
    assert _HINT not in response.text


@pytest.mark.parametrize(
    "base,return_to,expected",
    [
        ("https://app.example/", "/zh-CN/login", "https://app.example/zh-CN/login"),
        ("https://app.example/zh-CN", "/zh-CN/login", "https://app.example/zh-CN/login"),
        ("https://app.example/zh-CN/", "/en/login", "https://app.example/en/login"),
        ("https://app.example/en", "/en/login", "https://app.example/en/login"),
    ],
)
def test_end_session_frontend_base_url_origin(base, return_to, expected):
    host = MinimalOidcHost()
    host.config = lambda: replace(MinimalOidcHost().config(), frontend_base_url=base)
    host.stored_id_tokens["actor"] = _HINT
    client, _ = _client(host)
    response = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": return_to})
    assert response.status_code == 200
    assert response.json()["fields"]["post_logout_redirect_uri"] == expected


def test_end_session_404_without_hint_or_when_oidc_disabled():
    host = MinimalOidcHost()
    client, _ = _client(host)
    missing = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": "/zh-CN/login"})
    assert missing.status_code == 404
    assert missing.json() == {"code": "NO_END_SESSION"}
    host.stored_id_tokens["actor"] = _HINT
    host.config = lambda: replace(MinimalOidcHost().config(), enabled=False)
    disabled = client.post("/api/v1/auth/oidc/end-session", json={})
    assert disabled.status_code == 404
    assert disabled.json() == {"code": "NO_END_SESSION"}


def test_end_session_409_when_oidc_config_incomplete():
    host = MinimalOidcHost()
    host.stored_id_tokens["actor"] = _HINT
    host.config = lambda: replace(MinimalOidcHost().config(), frontend_base_url="")
    client, _ = _client(host)
    response = client.post("/api/v1/auth/oidc/end-session", json={})
    assert response.status_code == 409
    assert response.json()["kind"] == "not_configured"
    assert _HINT not in response.text


def test_end_session_requires_bearer():
    app = FastAPI()
    app.include_router(oidc.create_oidc_router(MinimalOidcHost()), prefix="/api/v1")
    client = TestClient(app)
    assert client.post("/api/v1/auth/oidc/end-session", json={}).status_code == 401
    assert (
        client.post("/api/v1/auth/oidc/end-session", headers={"Authorization": "Bearer ignored"}, json={}).status_code
        == 401
    )


def test_end_session_accepts_fastapi_request_dependency():
    def bearer(request: Request) -> str:
        return request.headers.get("authorization", "")

    def current_user(token: str = Depends(bearer)) -> CurrentUser:
        if token != "Bearer nested":
            raise HTTPException(401, "未登录")
        return _actor()

    host = MinimalOidcHost()
    host.stored_id_tokens["actor"] = _HINT
    client, _ = _client(host, current_user=current_user)
    denied = client.post("/api/v1/auth/oidc/end-session", json={})
    assert denied.status_code == 401
    ok = client.post("/api/v1/auth/oidc/end-session", headers={"Authorization": "Bearer nested"}, json={})
    assert ok.status_code == 200
    assert ok.json()["fields"]["id_token_hint"] == _HINT


def test_legacy_host_degrades_without_store_or_hint(monkeypatch):
    class LegacyHost:
        def config(self):
            return MinimalOidcHost().config()

        def upsert_identity(self, identity):
            return "actor"

        def issue_session(self, account_id):
            return "token"

        def revoke_sessions_by_subject(self, sub: str) -> int:
            return 0

    logged: list[str] = []

    def capture(message, *args, **_kwargs):
        logged.append(message % args if args else str(message))

    monkeypatch.setattr("enterprise_platform.oidc._LOG.warning", capture)
    host = LegacyHost()
    client, _ = _client(host)
    assert any("store_id_token" in line and "end_session_hint" in line for line in logged)
    monkeypatch.setattr(oidc, "exchange_code", lambda *_: {"id_token": _HINT, "access_token": ""})
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": "alice", "name": "Alice", "picture": "x"})
    authorize = client.get("/api/v1/auth/oidc/authorize", follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    login = client.get("/api/v1/auth/oidc/callback", params={"code": "ok", "state": state}, follow_redirects=False)
    assert login.status_code == 302
    missing = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": "/zh-CN/login"})
    assert missing.status_code == 404
    assert missing.json() == {"code": "NO_END_SESSION"}


def test_blank_adapter_stores_hint_and_backchannel_does_not_clear_it():
    from blank_app.database import SessionLocal
    from blank_app.models import Account
    from blank_app.oidc_adapter import BlankOidcHost

    host = BlankOidcHost()
    with SessionLocal() as db:
        oidc_account = Account(
            username=f"oidc-{uuid.uuid4().hex}",
            external_source="authentik",
            external_user_id=str(uuid.uuid4()),
            active=True,
        )
        local = Account(username=f"local-{uuid.uuid4().hex}", active=True)
        db.add_all([oidc_account, local])
        db.commit()
        oidc_id, local_id, sub = str(oidc_account.id), str(local.id), oidc_account.external_user_id
    assert Account.__table__.c.oidc_id_token.nullable is True
    assert host.end_session_hint(oidc_id) is None
    host.store_id_token(oidc_id, _HINT)
    assert host.end_session_hint(oidc_id) == _HINT
    host.store_id_token(local_id, _HINT)
    assert host.end_session_hint(local_id) is None
    assert host.revoke_sessions_by_subject(sub) == 1
    assert host.end_session_hint(oidc_id) == _HINT


def _blank_account(*, external: bool) -> str:
    from blank_app.database import SessionLocal
    from blank_app.models import Account

    with SessionLocal() as db:
        account = Account(
            username=f"{'oidc' if external else 'local'}-{uuid.uuid4().hex}",
            external_source="authentik" if external else None,
            external_user_id=str(uuid.uuid4()) if external else None,
            active=True,
        )
        db.add(account)
        db.commit()
        return str(account.id)


def _set_oidc_enabled(enabled: bool) -> dict:
    from blank_app.database import SessionLocal
    from blank_app.models import PlatformSetting
    from enterprise_platform.secrets import encrypt_secret

    with SessionLocal() as db:
        row = db.get(PlatformSetting, "oidc") or PlatformSetting(key="oidc")
        previous = dict(row.value or {})
        row.value = (
            {
                "enabled": True,
                "issuer": "https://auth.example/application/o/blank/",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "jwks_uri": "https://auth.example/jwks",
                "client_id": "blank",
                "client_secret": encrypt_secret("oidc-client-secret"),
                "redirect_uri": "https://blank.example/api/v1/auth/oidc/callback",
                "frontend_base_url": "https://blank.example",
            }
            if enabled
            else {}
        )
        db.add(row)
        db.commit()
    return previous


def test_blank_host_end_session_uses_bearer_and_404s_for_local():
    from blank_app.adapters import account_adapter
    from blank_app.main import app
    from blank_app.oidc_adapter import BlankOidcHost

    account_id = _blank_account(external=True)
    BlankOidcHost().store_id_token(account_id, _HINT)
    headers = {"Authorization": f"Bearer {account_adapter.issue_session(account_id)}"}
    local_headers = {"Authorization": f"Bearer {account_adapter.issue_session(_blank_account(external=False))}"}
    previous = _set_oidc_enabled(True)
    try:
        with TestClient(app) as client:
            ok = client.post("/api/v1/auth/oidc/end-session", headers=headers, json={"returnTo": "/zh-CN/login"})
            assert ok.status_code == 200
            assert ok.json() == {
                "url": "https://auth.example/application/o/blank/end-session/",
                "method": "POST",
                "fields": {
                    "id_token_hint": _HINT,
                    "post_logout_redirect_uri": "https://blank.example/zh-CN/login",
                },
            }
            _set_oidc_enabled(False)
            disabled = client.post("/api/v1/auth/oidc/end-session", headers=headers, json={"returnTo": "/en/login"})
            assert disabled.status_code == 404
            assert disabled.json() == {"code": "NO_END_SESSION"}
            _set_oidc_enabled(True)
            denied = client.post(
                "/api/v1/auth/oidc/end-session", headers=local_headers, json={"returnTo": "/zh-CN/login"}
            )
            assert denied.status_code == 404
            assert denied.json() == {"code": "NO_END_SESSION"}
    finally:
        _restore_oidc(previous)


def test_blank_end_session_401_for_missing_garbage_and_revoked_bearer():
    from blank_app.adapters import account_adapter
    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import Account
    from blank_app.oidc_adapter import BlankOidcHost

    account_id = _blank_account(external=True)
    BlankOidcHost().store_id_token(account_id, _HINT)
    token = account_adapter.issue_session(account_id)
    headers = {"Authorization": f"Bearer {token}"}
    previous = _set_oidc_enabled(True)
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/auth/oidc/end-session", json={}).status_code == 401
            assert (
                client.post(
                    "/api/v1/auth/oidc/end-session", headers={"Authorization": "Bearer not-a-jwt"}, json={}
                ).status_code
                == 401
            )
            with SessionLocal() as db:
                account = db.get(Account, uuid.UUID(account_id))
                account.sessions_revoked_at = datetime.now(UTC)
                db.commit()
            revoked = client.post("/api/v1/auth/oidc/end-session", headers=headers, json={"returnTo": "/zh-CN/login"})
            assert revoked.status_code == 401
            assert _HINT not in revoked.text
    finally:
        _restore_oidc(previous)


def test_blank_hint_omitted_from_me_session_and_local_accounts():
    from blank_app.adapters import account_adapter
    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import Account
    from blank_app.oidc_adapter import BlankOidcHost

    oidc_id = _blank_account(external=True)
    local_id = _blank_account(external=False)
    BlankOidcHost().store_id_token(oidc_id, _HINT)
    BlankOidcHost().store_id_token(local_id, _HINT)
    oidc_headers = {"Authorization": f"Bearer {account_adapter.issue_session(oidc_id)}"}
    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me", headers=oidc_headers)
        session = client.get("/api/v1/auth/session", headers=oidc_headers)
        assert me.status_code == 200
        assert session.status_code == 200
        assert "oidcIdToken" not in me.text
        assert "oidc_id_token" not in me.text
        assert _HINT not in me.text
        assert _HINT not in session.text
        with SessionLocal() as db:
            admin = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
            admin.must_change_password = False
            db.commit()
            admin_headers = {"Authorization": f"Bearer {account_adapter.issue_session(str(admin.id))}"}
        listed = client.get("/api/v1/local-accounts", headers=admin_headers)
        detail = client.get(f"/api/v1/local-accounts/{local_id}", headers=admin_headers)
        assert listed.status_code == 200
        assert detail.status_code == 200
        assert _HINT not in listed.text
        assert _HINT not in detail.text
        assert "oidcIdToken" not in listed.text
        assert "oidcIdToken" not in detail.text


def _restore_oidc(previous: dict) -> None:
    from blank_app.database import SessionLocal
    from blank_app.models import PlatformSetting

    with SessionLocal() as db:
        row = db.get(PlatformSetting, "oidc")
        if row is not None:
            row.value = previous
            db.commit()
