"""静默复检的状态绑定、本地化页面与完成片段契约。"""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from enterprise_platform import oidc
from platform_tests.test_shared_authorization_contract import MinimalOidcHost


class SilentHost(MinimalOidcHost):
    session_error = False

    def upsert_identity(self, identity):
        return f"local-{identity.sub}"

    def issue_session(self, account_id):
        if self.session_error:
            raise oidc.OidcFlowError("session failure", kind="inactive_user")
        return f"jwt-for-{account_id}"


@pytest.fixture
def silent_client(monkeypatch):
    host = SilentHost()
    routes = oidc.OidcRouteConfig(api_base_path="/gateway/v2", frontend_silent_path="/login/oidc-silent")
    app = FastAPI()
    app.include_router(oidc.create_oidc_router(host, routes=routes), prefix=routes.api_base_path)
    monkeypatch.setattr(oidc, "exchange_code", lambda *_: {"id_token": "provider-token"})
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": "alice", "name": "Alice", "picture": "avatar"})
    with TestClient(app) as client:
        yield client, host, routes


def _authorize(client, query="silent=1&locale=en"):
    response = client.get(f"/gateway/v2/auth/oidc/authorize?{query}", follow_redirects=False)
    assert response.status_code == 302
    return httpx.URL(response.headers["location"]).params


def _callback(client, state, **params):
    return client.get("/gateway/v2/auth/oidc/callback", params={"state": state, **params}, follow_redirects=False)


def _assert_silent(response, expected, locale="en"):
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.scheme == "https" and location.netloc == "app.example"
    assert location.path == f"/{locale}/login/oidc-silent"
    assert location.query == ""
    assert parse_qs(location.fragment) == {key: [value] for key, value in expected.items()}
    assert response.headers["cache-control"] == "no-store"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Path=/gateway/v2/auth/oidc" in response.headers["set-cookie"]


def test_silent_authorize_sets_signed_claim_and_prompt(silent_client):
    client, host, routes = silent_client
    status = client.get("/gateway/v2/auth/oidc/status").json()
    assert status["silentAuthorizePath"] == status["authorizePath"] + "?silent=1"
    params = _authorize(client)
    assert params["prompt"] == "none"
    assert params["code_challenge_method"] == "S256"
    claims = oidc.verify_state(client.cookies.get(routes.state_cookie_name), params["state"], host.config())
    assert claims["silent"] is True
    assert claims["locale"] == "en"
    ordinary = _authorize(client, "locale=en")
    assert "prompt" not in ordinary
    claims = oidc.verify_state(client.cookies.get(routes.state_cookie_name), ordinary["state"], host.config())
    assert claims["silent"] is False


@pytest.mark.parametrize("error", ["login_required", "interaction_required", "consent_required", "access_denied"])
def test_silent_provider_logged_out(silent_client, error):
    client, _, _ = silent_client
    state = _authorize(client)["state"]
    response = _callback(client, state, error=error, error_description="provider-secret")
    _assert_silent(response, {"outcome": "logged_out", "kind": error})
    assert "provider-secret" not in response.headers["location"]


@pytest.mark.parametrize("error,kind", [("server_error", "server_error"), ("unknown", "provider_error")])
def test_silent_provider_failure_is_error(silent_client, error, kind):
    client, _, _ = silent_client
    state = _authorize(client)["state"]
    _assert_silent(_callback(client, state, error=error), {"outcome": "error", "kind": kind})


@pytest.mark.parametrize("subject", ["alice", "bob"])
def test_silent_success_reports_current_account_and_new_session(silent_client, monkeypatch, subject):
    client, _, _ = silent_client
    monkeypatch.setattr(oidc, "validate_id_token", lambda *_: {"sub": subject, "name": subject, "picture": "avatar"})
    state = _authorize(client, "silent=1&next=/zh-CN/settings")["state"]
    _assert_silent(
        _callback(client, state, code="code"),
        {
            "outcome": "authenticated",
            "token": f"jwt-for-local-{subject}",
            "account": f"local-{subject}",
        },
        locale="zh-CN",
    )


@pytest.mark.parametrize(
    "stage,kind",
    [
        ("code", "missing_code"),
        ("exchange", "token_error"),
        ("token", "invalid_token"),
        ("session", "inactive_user"),
        ("state", "state_mismatch"),
    ],
)
def test_silent_flow_errors_clear_cookie(silent_client, monkeypatch, stage, kind):
    client, host, _ = silent_client
    state = _authorize(client)["state"]

    def fail(*_args):
        raise oidc.OidcFlowError("private error", kind=kind)

    if stage == "exchange":
        monkeypatch.setattr(oidc, "exchange_code", fail)
    if stage == "token":
        monkeypatch.setattr(oidc, "validate_id_token", fail)
    host.session_error = stage == "session"
    response = _callback(
        client, "wrong-state" if stage == "state" else state, **({} if stage == "code" else {"code": "code"})
    )
    _assert_silent(response, {"outcome": "error", "kind": kind})


def test_untrusted_silent_claim_cannot_choose_callback_page(silent_client):
    client, _, routes = silent_client
    params = _authorize(client)
    forged = jwt.encode({"silent": True, "state": params["state"]}, "attacker-key", algorithm="HS256")
    client.cookies.clear()
    client.cookies.set(routes.state_cookie_name, forged)
    response = _callback(client, params["state"], error="login_required")
    assert urlsplit(response.headers["location"]).path == "/zh-CN/login"
    assert "state_mismatch" in response.headers["location"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_ordinary_success_contract_and_unconfigured_cookie_cleanup(silent_client):
    client, host, _ = silent_client
    state = _authorize(client, "next=/en/settings")["state"]
    response = _callback(client, state, code="code")
    location = urlsplit(response.headers["location"])
    assert location.path == "/en/login/oidc-complete"
    assert parse_qs(location.fragment) == {"token": ["jwt-for-local-alice"], "next": ["/en/settings"]}
    state = _authorize(client)["state"]
    config = replace(host.config(), enabled=False)
    host.config = lambda: config
    response = _callback(client, state, code="code")
    assert response.status_code == 404
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_expired_signed_silent_state_returns_error_without_issuing_session(silent_client):
    client, host, routes = silent_client
    state = _authorize(client)["state"]
    claims = oidc.verify_state(client.cookies.get(routes.state_cookie_name), state, host.config())
    claims["exp"] = 1
    cookie = jwt.encode(claims, host.config().signing_secret, algorithm="HS256")
    client.cookies.clear()
    client.cookies.set(routes.state_cookie_name, cookie)
    _assert_silent(_callback(client, state, code="code"), {"outcome": "error", "kind": "state_mismatch"})
