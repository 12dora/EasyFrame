"""认证热路径:请求内 memo、/notifications 双 Depends、worker 解析。"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from blank_app.adapter_support import request_token
from blank_app.adapters import account_adapter
from blank_app.authz_hotpath import load_authenticated_account
from enterprise_platform.auth import AuthError
from enterprise_platform.request_scope import begin_request_scope, end_request_scope
from enterprise_platform.schemas import CurrentUser
from platform_tests.test_blank_app_api import _login, _reset_admin

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


@pytest.fixture
def client() -> Iterator[TestClient]:
    from blank_app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _admin_headers(client: TestClient) -> dict[str, str]:
    _reset_admin()
    login = _login(client)
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['accessToken']}"}


def test_web_workers_default_and_validation(monkeypatch) -> None:
    from blank_app.run import web_workers

    monkeypatch.delenv("BLANK_WEB_WORKERS", raising=False)
    assert web_workers() == 2
    monkeypatch.setenv("BLANK_WEB_WORKERS", "4")
    assert web_workers() == 4
    for invalid in ("0", "-1", "many"):
        monkeypatch.setenv("BLANK_WEB_WORKERS", invalid)
        with pytest.raises(RuntimeError, match="BLANK_WEB_WORKERS"):
            web_workers()


def test_run_main_starts_import_string_with_workers(monkeypatch) -> None:
    from blank_app import run

    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("BLANK_WEB_WORKERS", "3")
    monkeypatch.delenv("BLANK_HOST", raising=False)
    monkeypatch.delenv("BLANK_PORT", raising=False)
    monkeypatch.setattr(run.command, "upgrade", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run.uvicorn, "run", lambda target, **kwargs: calls.append((target, kwargs)))
    run.main()
    assert calls == [("blank_app.main:app", {"host": "0.0.0.0", "port": 8000, "workers": 3})]


def test_current_user_resolves_once_per_request(monkeypatch, client: TestClient) -> None:
    calls = {"n": 0}
    original = load_authenticated_account

    def wrapped(db):
        calls["n"] += 1
        return original(db)

    monkeypatch.setattr("blank_app.authz_hotpath.load_authenticated_account", wrapped)
    headers = _admin_headers(client)
    token = request_token.set(headers["Authorization"].split(" ", 1)[1])
    scope = begin_request_scope()
    try:
        first = account_adapter.current_user()
        second = account_adapter.current_user()
        assert first is second
        assert calls["n"] == 1
    finally:
        end_request_scope(scope)
        request_token.reset(token)


def test_current_user_not_memoised_outside_request(monkeypatch, client: TestClient) -> None:
    calls = {"n": 0}
    original = load_authenticated_account

    def wrapped(db):
        calls["n"] += 1
        return original(db)

    monkeypatch.setattr("blank_app.authz_hotpath.load_authenticated_account", wrapped)
    headers = _admin_headers(client)
    token = request_token.set(headers["Authorization"].split(" ", 1)[1])
    try:
        account_adapter.current_user()
        account_adapter.current_user()
        assert calls["n"] == 2
    finally:
        request_token.reset(token)


def test_notifications_double_depends_resolve_once(monkeypatch, client: TestClient) -> None:
    calls = {"n": 0}
    original = load_authenticated_account

    def wrapped(db):
        calls["n"] += 1
        return original(db)

    monkeypatch.setattr("blank_app.authz_hotpath.load_authenticated_account", wrapped)
    response = client.get("/api/v1/notifications", headers=_admin_headers(client))
    assert response.status_code == 200, response.text
    assert calls["n"] == 1


def test_auth_failure_is_not_cached(monkeypatch) -> None:
    calls = {"n": 0}
    original = load_authenticated_account

    def wrapped(db):
        calls["n"] += 1
        return original(db)

    monkeypatch.setattr("blank_app.authz_hotpath.load_authenticated_account", wrapped)
    token = request_token.set("not-a-jwt")
    scope = begin_request_scope()
    try:
        with pytest.raises(AuthError):
            account_adapter.current_user()
        with pytest.raises(AuthError):
            account_adapter.current_user()
        assert calls["n"] == 2
    finally:
        end_request_scope(scope)
        request_token.reset(token)


def test_revocation_is_enforced_on_the_next_request(client: TestClient) -> None:
    headers = _admin_headers(client)
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    account_adapter.revoke_sessions(me.json()["id"])
    denied = client.get("/api/v1/auth/me", headers=headers)
    assert denied.status_code == 401


def test_issue_session_drops_memo(monkeypatch, client: TestClient) -> None:
    calls = {"n": 0}
    original = load_authenticated_account

    def wrapped(db):
        calls["n"] += 1
        return original(db)

    monkeypatch.setattr("blank_app.authz_hotpath.load_authenticated_account", wrapped)
    headers = _admin_headers(client)
    raw = headers["Authorization"].split(" ", 1)[1]
    token = request_token.set(raw)
    scope = begin_request_scope()
    try:
        user = account_adapter.current_user()
        assert calls["n"] == 1
        account_adapter.issue_session(user.id)
        account_adapter.current_user()
        assert calls["n"] == 2
    finally:
        end_request_scope(scope)
        request_token.reset(token)


def test_revoke_sessions_drops_memo_so_second_current_user_sees_401(client: TestClient) -> None:
    headers = _admin_headers(client)
    raw = headers["Authorization"].split(" ", 1)[1]
    user = _current_user_in_scope(raw)
    token = request_token.set(raw)
    scope = begin_request_scope()
    try:
        account_adapter.current_user()
        account_adapter.revoke_sessions(user.id)
        with pytest.raises(AuthError) as caught:
            account_adapter.current_user()
        assert caught.value.status_code == 401
    finally:
        end_request_scope(scope)
        request_token.reset(token)


def test_change_password_drops_memo(client: TestClient) -> None:
    headers = _admin_headers(client)
    raw = headers["Authorization"].split(" ", 1)[1]
    user = _current_user_in_scope(raw)
    token = request_token.set(raw)
    scope = begin_request_scope()
    try:
        account_adapter.current_user()
        assert account_adapter.change_password(user.id, os.environ["BLANK_ADMIN_PASSWORD"], "temporary-password-43!")
        with pytest.raises(AuthError) as caught:
            account_adapter.current_user()
        assert caught.value.status_code == 401
    finally:
        end_request_scope(scope)
        request_token.reset(token)
        _reset_admin()


def test_require_permission_with_user_does_not_resolve_again(monkeypatch) -> None:
    def boom() -> CurrentUser:
        raise AssertionError("must reuse passed user")

    monkeypatch.setattr(account_adapter, "current_user", boom)
    from blank_app.adapter_account import require_permission

    user = CurrentUser(id="user-1", name="alice", permissions=["notification.center.view"])
    require_permission("notification.center.view", user=user)
    with pytest.raises(AuthError) as caught:
        require_permission("accounts.local.view", user=user)
    assert caught.value.status_code == 403
    assert caught.value.detail == "缺少权限"


def _current_user_in_scope(raw: str) -> CurrentUser:
    token = request_token.set(raw)
    scope = begin_request_scope()
    try:
        return account_adapter.current_user()
    finally:
        end_request_scope(scope)
        request_token.reset(token)
