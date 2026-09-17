"""默认 permission_for 与 current_user 共用 Depends,拒绝仍 403 并写审计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from enterprise_platform.assembly import (
    PlatformPorts,
    PlatformRouteGroups,
    create_platform_router,
)
from enterprise_platform.auth import AuthError
from enterprise_platform.schemas import CurrentUser, NotificationPage

NOTIFICATION_VIEW = "notification.center.view"


@dataclass
class _SpyAccount:
    calls: int = 0
    permissions: list[str] = field(default_factory=lambda: [NOTIFICATION_VIEW])
    must_change_password: bool = False

    def current_user(self) -> CurrentUser:
        self.calls += 1
        return CurrentUser(
            id="user-1",
            name="alice",
            must_change_password=self.must_change_password,
            permissions=list(self.permissions),
        )


class _Notifications:
    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        del user_id, cursor, limit
        return NotificationPage(items=[], unread_count=0, next_cursor=None)


def _client(
    account: object,
    require_permission: Any,
    *,
    factory: Any = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=account,
                app_settings=None,
                notifications=_Notifications(),
                integrations=None,
                directory=None,
                upstream_health=None,
                require_permission=require_permission,
            ),
            include_authz_integration=False,
            permission_dependency_factory=factory,
            route_groups=PlatformRouteGroups(
                passkeys=False,
                footer=False,
                identity=False,
                easyauth=False,
                upstream=False,
            ),
        ),
        prefix="/api/v1",
    )
    return TestClient(app)


def test_notifications_double_depends_resolve_current_user_once() -> None:
    account = _SpyAccount()
    client = _client(account, lambda _code: None)
    response = client.get("/api/v1/notifications")
    assert response.status_code == 200, response.text
    assert account.calls == 1


def test_denied_permission_is_403_and_writes_authorization_denied_audit() -> None:
    account = _SpyAccount(permissions=[])
    audits: list[dict[str, object]] = []

    def require_permission(code: str, request: Request | None = None, *, user: CurrentUser | None = None) -> None:
        assert user is not None
        assert user.id == "user-1"
        assert account.calls == 1
        after: dict[str, object] = {"permission": code, "source": "require_permission"}
        if request is not None:
            after["method"] = request.method
            after["route"] = request.url.path
        audits.append({"actor": user.id, "action": "authorization.denied", "after": after})
        raise AuthError(403, "缺少权限")

    client = _client(account, require_permission)
    response = client.get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert account.calls == 1
    assert audits == [
        {
            "actor": "user-1",
            "action": "authorization.denied",
            "after": {
                "permission": NOTIFICATION_VIEW,
                "source": "require_permission",
                "method": "GET",
                "route": "/api/v1/notifications",
            },
        }
    ]


def test_legacy_require_permission_code_only_still_allows() -> None:
    account = _SpyAccount()
    seen: list[str] = []

    def require_permission(code: str) -> None:
        seen.append(code)

    client = _client(account, require_permission)
    response = client.get("/api/v1/notifications")
    assert response.status_code == 200, response.text
    assert account.calls == 1
    assert seen == []


def test_legacy_require_permission_code_only_still_denies() -> None:
    account = _SpyAccount(permissions=[])
    seen: list[str] = []

    def require_permission(code: str) -> None:
        seen.append(code)
        user = account.current_user()
        if code not in user.permissions:
            raise AuthError(403, "缺少权限")

    client = _client(account, require_permission)
    response = client.get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert seen == [NOTIFICATION_VIEW]
    assert account.calls == 2


def test_custom_permission_factory_is_byte_compatible() -> None:
    account = _SpyAccount(permissions=[])
    seen: list[str] = []

    def factory(code: str):
        seen.append(code)

        def check() -> None:
            return None

        return check

    client = _client(account, lambda _code: None, factory=factory)
    response = client.get("/api/v1/notifications")
    assert response.status_code == 200, response.text
    assert NOTIFICATION_VIEW in seen
    assert account.calls == 1


def test_must_change_password_wins_over_missing_permission() -> None:
    account = _SpyAccount(permissions=[], must_change_password=True)
    seen: list[str] = []

    def require_permission(code: str, request: Request | None = None, *, user: CurrentUser | None = None) -> None:
        del request, user
        seen.append(code)
        raise AuthError(403, "缺少权限")

    response = _client(account, require_permission).get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "PASSWORD_CHANGE_REQUIRED"}
    assert seen == []
    assert account.calls == 1


def test_must_change_password_auth_me_still_200() -> None:
    account = _SpyAccount(must_change_password=True)
    seen: list[str] = []

    def require_permission(code: str) -> None:
        seen.append(code)

    response = _client(account, require_permission).get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["mustChangePassword"] is True
    assert seen == []


def test_unauthenticated_permission_route_is_401() -> None:
    class _Guest:
        def current_user(self) -> CurrentUser:
            raise AuthError(401, "未登录")

    seen: list[str] = []

    def require_permission(code: str) -> None:
        seen.append(code)
        raise AuthError(403, "缺少权限")

    response = _client(_Guest(), require_permission).get("/api/v1/notifications")
    assert response.status_code == 401
    assert response.json()["detail"] == "未登录"
    assert seen == []


def test_bound_method_port_denies_with_user_and_audit() -> None:
    account = _SpyAccount(permissions=[])

    class _Port:
        def __init__(self) -> None:
            self.audits: list[str] = []

        def require_permission(
            self, code: str, request: Request | None = None, *, user: CurrentUser | None = None
        ) -> None:
            assert user is not None
            assert request is not None
            self.audits.append(code)
            raise AuthError(403, "缺少权限")

    port = _Port()
    response = _client(account, port.require_permission).get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert port.audits == [NOTIFICATION_VIEW]


def test_partial_port_denies_with_user_and_audit() -> None:
    account = _SpyAccount(permissions=[])
    audits: list[str] = []

    def require_permission(
        code: str,
        request: Request | None = None,
        *,
        user: CurrentUser | None = None,
        tag: str = "",
    ) -> None:
        assert tag == "partial"
        assert user is not None
        assert request is not None
        audits.append(code)
        raise AuthError(403, "缺少权限")

    response = _client(account, partial(require_permission, tag="partial")).get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert audits == [NOTIFICATION_VIEW]


def test_explicit_user_keyword_only_port_receives_user() -> None:
    account = _SpyAccount(permissions=[])
    seen: list[CurrentUser | None] = []

    def require_permission(code: str, *, user: CurrentUser | None = None) -> None:
        del code
        seen.append(user)
        raise AuthError(403, "缺少权限")

    response = _client(account, require_permission).get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert seen[0] is not None
    assert seen[0].id == "user-1"


def test_kwargs_wrapper_around_legacy_port_still_denies_and_audits() -> None:
    account = _SpyAccount(permissions=[])
    audits: list[str] = []

    def orig(code: str) -> None:
        audits.append(code)
        raise AuthError(403, "缺少权限")

    def wrapper(*args: Any, **kwargs: Any) -> None:
        return orig(*args, **kwargs)

    response = _client(account, wrapper).get("/api/v1/notifications")
    assert response.status_code == 403
    assert response.json()["detail"] == "缺少权限"
    assert audits == [NOTIFICATION_VIEW]


def test_host_typeerror_after_audit_is_not_retried() -> None:
    account = _SpyAccount(permissions=[])
    audits: list[str] = []

    def require_permission(code: str, request: Request | None = None, *, user: CurrentUser | None = None) -> None:
        del request, user
        audits.append(code)
        raise TypeError("host body failed")

    client = _client(account, require_permission)
    with pytest.raises(TypeError, match="host body failed"):
        client.get("/api/v1/notifications")
    assert audits == [NOTIFICATION_VIEW]
