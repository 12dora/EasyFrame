"""assembly 依赖对象必须按身份哈希:宿主 adapter 不可哈希时路由仍可用。"""

from dataclasses import dataclass, field
from inspect import signature

from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.assembly.contracts import PlatformSecurityHooks
from enterprise_platform.assembly.dependencies import build_assembly_dependencies
from enterprise_platform.schemas import CurrentUser, NotificationPage


@dataclass
class _MutableAccountPort:
    """eq=True 且非 frozen 的 dataclass 没有 __hash__,模拟宿主里带状态的 adapter。"""

    calls: list[str] = field(default_factory=list)

    def current_user(self) -> CurrentUser:
        self.calls.append("current_user")
        return CurrentUser(
            id="user-1",
            name="alice",
            must_change_password=False,
            permissions=["notification.center.view"],
        )


class _Notifications:
    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        del user_id, cursor, limit
        return NotificationPage(items=[], unread_count=0, next_cursor=None)


def _ports(account: _MutableAccountPort | None = None) -> PlatformPorts:
    return PlatformPorts(
        account=account or _MutableAccountPort(),
        app_settings=None,
        notifications=_Notifications(),
        integrations=None,
        directory=None,
        upstream_health=None,
        require_permission=lambda _code: None,
    )


def _app_with_unhashable_adapter() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            _ports(),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                passkeys=False,
                footer=False,
                notifications=False,
                identity=False,
                easyauth=False,
                upstream=False,
            ),
        ),
        prefix="/api/v1",
    )
    return app


def test_default_user_and_permission_dependencies_tolerate_unhashable_adapters() -> None:
    client = TestClient(_app_with_unhashable_adapter())
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "alice"


def test_app_settings_route_group_flag_unregisters_general_and_footer() -> None:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            _ports(),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                passkeys=False,
                app_settings=False,
                notifications=False,
                identity=False,
                easyauth=False,
                upstream=False,
            ),
        ),
        prefix="/api/v1",
    )
    client = TestClient(app)
    assert client.get("/api/v1/app-settings/general").status_code == 404
    assert client.get("/api/v1/app-settings/footer").status_code == 404


def test_default_permission_depends_on_assembly_current_user() -> None:
    ctx = build_assembly_dependencies(
        _ports(),
        hooks=PlatformSecurityHooks(),
        current_user_dependency=None,
        permission_dependency_factory=None,
        enforce_shared_rate_limits=False,
        include_authz_integration=False,
    )
    check = ctx.permission_for("notification.center.view")
    user_default = signature(check).parameters["user"].default
    assert user_default.dependency is ctx.current_user
    other = ctx.permission_for("settings.app_setting.update")
    assert check is not other


def test_unhashable_adapter_notifications_route_resolves_user_once() -> None:
    account = _MutableAccountPort()
    app = FastAPI()
    app.include_router(
        create_platform_router(
            _ports(account),
            include_authz_integration=False,
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
    response = TestClient(app).get("/api/v1/notifications")
    assert response.status_code == 200, response.text
    assert account.calls == ["current_user"]


def test_permission_dependency_factory_is_returned_unchanged() -> None:
    sentinel = object()

    def factory(code: str):
        assert code == "notification.center.view"
        return sentinel

    ctx = build_assembly_dependencies(
        _ports(),
        hooks=PlatformSecurityHooks(),
        current_user_dependency=None,
        permission_dependency_factory=factory,
        enforce_shared_rate_limits=False,
        include_authz_integration=False,
    )
    assert ctx.permission_for("notification.center.view") is sentinel
