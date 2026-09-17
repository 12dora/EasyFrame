"""默认 permission_for 与 current_user 共用 Depends,拒绝仍 403 并写审计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

    def current_user(self) -> CurrentUser:
        self.calls += 1
        return CurrentUser(id="user-1", name="alice", must_change_password=False, permissions=list(self.permissions))


class _Notifications:
    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        del user_id, cursor, limit
        return NotificationPage(items=[], unread_count=0, next_cursor=None)


def _client(
    account: _SpyAccount,
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
