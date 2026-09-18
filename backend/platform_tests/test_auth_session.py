"""GET /auth/session and PATCH /auth/preferences: login-gated UI session contract."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from enterprise_platform.assembly import (
    PlatformPorts,
    PlatformRouteGroups,
    PlatformSecurityHooks,
    create_platform_router,
)
from enterprise_platform.auth import AuthError
from enterprise_platform.schemas import CurrentUser, EasyAuthStatus

_COMPACT = {"tableDensity": "compact"}
_COMFORTABLE = {"tableDensity": "comfortable"}


class _Account:
    def current_user(self) -> CurrentUser:
        return CurrentUser(id="guest-1", name="guest", must_change_password=False, permissions=[])


class _PrefAccount:
    def __init__(self, prefs: dict[str, Any] | None = None) -> None:
        self.prefs = dict(prefs or {})

    def current_user(self) -> CurrentUser:
        return CurrentUser(id="user-1", name="guest", must_change_password=False, permissions=[])

    def get_ui_preferences(self, account_id: str) -> dict[str, Any]:
        del account_id
        return dict(self.prefs)

    def update_ui_preferences(self, account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        del account_id
        self.prefs.update(patch)
        return dict(self.prefs)


class _Integrations:
    def __init__(self, url: str = "", *, fail: bool = False) -> None:
        self.url = url
        self.fail = fail

    def get_easyauth_status(self) -> EasyAuthStatus:
        if self.fail:
            raise HTTPException(503, "easyauth unavailable")
        return EasyAuthStatus(permission_request_url=self.url)


def _session_json(url: str | None, density: str = "compact") -> dict[str, Any]:
    return {"permissionRequestUrl": url, "preferences": {"tableDensity": density}}


def _client(
    *,
    account: object | None = None,
    integrations: object | None = None,
    me: bool = True,
    hooks: PlatformSecurityHooks | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=account or _Account(),
                app_settings=None,
                notifications=None,
                integrations=integrations,
                directory=None,
                upstream_health=None,
                require_permission=lambda _code: None,
            ),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                passkeys=False,
                footer=False,
                notifications=False,
                identity=False,
                easyauth=False,
                upstream=False,
                me=me,
            ),
            security_hooks=hooks or PlatformSecurityHooks(),
        ),
        prefix="/api/v1",
    )
    return TestClient(app)


def test_session_returns_configured_url_without_a_permission_code() -> None:
    client = _client(integrations=_Integrations("https://easyauth.example.test/portal/request"))
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200, response.text
    assert response.json() == _session_json("https://easyauth.example.test/portal/request")


def test_session_url_blank_or_missing_is_null() -> None:
    assert _client(integrations=_Integrations("")).get("/api/v1/auth/session").json() == _session_json(None)
    assert _client(integrations=_Integrations("   ")).get("/api/v1/auth/session").json() == _session_json(None)
    assert _client(integrations=None).get("/api/v1/auth/session").json() == _session_json(None)


def test_session_does_not_fail_open_when_easyauth_status_errors() -> None:
    response = _client(integrations=_Integrations(fail=True)).get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json() == _session_json(None)


def test_session_rejects_unauthenticated_caller() -> None:
    class _Guest:
        def current_user(self) -> CurrentUser:
            raise AuthError(401, "未登录")

    response = _client(account=_Guest()).get("/api/v1/auth/session")
    assert response.status_code == 401


def test_session_follows_the_me_route_group() -> None:
    assert _client(me=False).get("/api/v1/auth/session").status_code == 404
    assert _client(me=False).get("/api/v1/auth/me").status_code == 404
    assert _client(me=False).patch("/api/v1/auth/preferences", json=_COMFORTABLE).status_code == 404


def test_session_preferences_default_compact_when_key_absent() -> None:
    response = _client().get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json()["preferences"] == _COMPACT


def test_preferences_patch_persists_and_reflects_on_session() -> None:
    account = _PrefAccount()
    client = _client(account=account)
    patched = client.patch("/api/v1/auth/preferences", json=_COMFORTABLE)
    assert patched.status_code == 200, patched.text
    assert patched.json()["preferences"] == _COMFORTABLE
    assert account.prefs == {"table_density": "comfortable"}
    assert client.get("/api/v1/auth/session").json()["preferences"] == _COMFORTABLE


def test_preferences_patch_rejects_invalid_value() -> None:
    response = _client(account=_PrefAccount()).patch("/api/v1/auth/preferences", json={"tableDensity": "wide"})
    assert response.status_code == 422


def test_preferences_patch_rejects_unknown_keys() -> None:
    response = _client(account=_PrefAccount()).patch("/api/v1/auth/preferences", json={"rowHeight": "compact"})
    assert response.status_code == 422


def test_preferences_patch_rejects_unauthenticated_caller() -> None:
    class _Guest:
        def current_user(self) -> CurrentUser:
            raise AuthError(401, "未登录")

    response = _client(account=_Guest()).patch("/api/v1/auth/preferences", json=_COMFORTABLE)
    assert response.status_code == 401


def test_preferences_patch_writes_audit_like_other_account_mutations() -> None:
    events: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def after_event(actor_id: str, action: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
        events.append((actor_id, action, before, after))

    client = _client(account=_PrefAccount(), hooks=PlatformSecurityHooks(after_event=after_event))
    response = client.patch("/api/v1/auth/preferences", json=_COMFORTABLE)
    assert response.status_code == 200
    assert events == [
        ("user-1", "auth.preferences.update", {"tableDensity": "compact"}, {"tableDensity": "comfortable"})
    ]
