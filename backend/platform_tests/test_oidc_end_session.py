"""RP 发起注销：回调落库 id_token，end-session 返回 Authentik POST 表单字段。"""

import uuid
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform import oidc
from enterprise_platform.schemas import CurrentUser
from platform_tests.test_shared_authorization_contract import MinimalOidcHost

_HINT = "raw-id-token.with.dingtalk-claims"


def _actor() -> CurrentUser:
    return CurrentUser(id="actor", name="alice")


def _client(host: MinimalOidcHost | None = None, current_user=_actor):
    host = host or MinimalOidcHost()
    app = FastAPI()
    app.include_router(oidc.create_oidc_router(host, current_user_dependency=current_user), prefix="/api/v1")
    return TestClient(app), host


def _complete_login(client, monkeypatch, host, id_token=_HINT):
    monkeypatch.setattr(oidc, "exchange_code", lambda *_: {"id_token": id_token, "access_token": ""})
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": "alice", "name": "Alice", "picture": "x"})
    authorize = client.get("/api/v1/auth/oidc/authorize", follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    response = client.get("/api/v1/auth/oidc/callback", params={"code": "ok", "state": state}, follow_redirects=False)
    assert response.status_code == 302
    assert host.stored_id_tokens["actor"] == id_token


def test_callback_stores_id_token(monkeypatch):
    host = MinimalOidcHost()
    client, _ = _client(host)
    _complete_login(client, monkeypatch, host)


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
    ["", "login", "//evil.example/login", "https://evil.example/login", "/foo\\bar", "/foo://bar", "/\nlogin"],
)
def test_end_session_rejects_unsafe_return_to(return_to):
    host = MinimalOidcHost()
    host.stored_id_tokens["actor"] = _HINT
    client, _ = _client(host)
    response = client.post("/api/v1/auth/oidc/end-session", json={"returnTo": return_to})
    assert response.status_code == 422
    assert _HINT not in response.text


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


def test_end_session_requires_bearer():
    app = FastAPI()
    app.include_router(oidc.create_oidc_router(MinimalOidcHost()), prefix="/api/v1")
    assert TestClient(app).post("/api/v1/auth/oidc/end-session", json={}).status_code == 401


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


def _restore_oidc(previous: dict) -> None:
    from blank_app.database import SessionLocal
    from blank_app.models import PlatformSetting

    with SessionLocal() as db:
        row = db.get(PlatformSetting, "oidc")
        if row is not None:
            row.value = previous
            db.commit()
