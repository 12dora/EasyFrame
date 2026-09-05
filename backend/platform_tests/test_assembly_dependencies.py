"""assembly 依赖对象必须按身份哈希:宿主 adapter 不可哈希时路由仍可用。"""

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.schemas import CurrentUser


@dataclass
class _MutableAccountPort:
    """eq=True 且非 frozen 的 dataclass 没有 __hash__,模拟宿主里带状态的 adapter。"""

    calls: list[str] = field(default_factory=list)

    def current_user(self) -> CurrentUser:
        self.calls.append("current_user")
        return CurrentUser(id="user-1", name="alice", must_change_password=False)


def _app_with_unhashable_adapter() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=_MutableAccountPort(),
                footer=None,
                notifications=None,
                integrations=None,
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
