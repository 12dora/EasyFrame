"""platform_tests 共享前置。"""

import pytest


@pytest.fixture(scope="session")
def blank_admin_seeded() -> None:
    """确保 blank_app 的 lifespan 至少跑过一次,admin 账号已落库。

    admin 不是迁移建出来的,而是 blank_app 启动时 seed 的。多条用例会在进入
    ``TestClient(app)`` 之前直接查询或改写 admin(``_reset_admin``、
    ``test_session_and_oidc_state_keys_are_separated_and_rotatable`` 等),
    没有这个夹具时它们只在「某个更早的用例已经启动过 app」的顺序下才通过,
    换个执行顺序就 ``NoResultFound``。前置状态应当由用例自己建立,不能靠执行顺序。

    刻意不设 autouse:它需要 BLANK_* 环境变量,而 ``test_platform_core.py`` /
    ``test_shared_authorization_contract.py`` 是纯共享层用例,不该被拖上这个依赖。
    需要 blank 宿主的模块用 ``pytestmark = pytest.mark.usefixtures(...)`` 整模块声明。
    """

    from fastapi.testclient import TestClient

    from blank_app.main import app

    with TestClient(app):
        pass
