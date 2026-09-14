"""GET /auth/session:登录即可达,只返回权限申请入口。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.schemas import CurrentUser, EasyAuthStatus


class _Account:
    def current_user(self) -> CurrentUser:
        return CurrentUser(id="guest-1", name="guest", must_change_password=False, permissions=[])


class _Integrations:
    def __init__(self, url: str = "", *, fail: bool = False) -> None:
        self.url = url
        self.fail = fail

    def get_easyauth_status(self) -> EasyAuthStatus:
        if self.fail:
            raise HTTPException(503, "easyauth unavailable")
        return EasyAuthStatus(permission_request_url=self.url)


def _client(*, integrations: object | None = None, me: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=_Account(),
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
        ),
        prefix="/api/v1",
    )
    return TestClient(app)


def test_session_returns_configured_url_without_a_permission_code() -> None:
    client = _client(integrations=_Integrations("https://easyauth.example.test/portal/request"))
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200, response.text
    assert response.json() == {"permissionRequestUrl": "https://easyauth.example.test/portal/request"}


def test_session_url_blank_or_missing_is_null() -> None:
    assert _client(integrations=_Integrations("")).get("/api/v1/auth/session").json() == {"permissionRequestUrl": None}
    assert _client(integrations=_Integrations("   ")).get("/api/v1/auth/session").json() == {
        "permissionRequestUrl": None
    }
    assert _client(integrations=None).get("/api/v1/auth/session").json() == {"permissionRequestUrl": None}


def test_session_does_not_fail_open_when_easyauth_status_errors() -> None:
    response = _client(integrations=_Integrations(fail=True)).get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json() == {"permissionRequestUrl": None}


def test_session_follows_the_me_route_group() -> None:
    assert _client(me=False).get("/api/v1/auth/session").status_code == 404
    assert _client(me=False).get("/api/v1/auth/me").status_code == 404
