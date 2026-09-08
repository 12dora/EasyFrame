import base64
import os
from datetime import datetime
from types import SimpleNamespace

import pyotp
import pytest
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url

# 整模块依赖 blank 宿主已 seed 出 admin;多条用例在进入 TestClient 之前就直接改 admin。
pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

AUTHORIZATION_ROUTE_CONTRACT = (
    ("/api/v1/authz-integration/status", "GET"),
    ("/api/v1/authz-integration/connection-test", "POST"),
    ("/api/v1/authz-integration/settings", "GET"),
    ("/api/v1/authz-integration/settings", "PUT"),
    ("/api/v1/authz-integration/permission-catalog", "GET"),
    ("/api/v1/authz-integration/snapshots", "GET"),
    ("/api/v1/authz-integration/snapshots/{user_id}/refresh", "POST"),
    ("/api/v1/authz-integration/manifest", "GET"),
    ("/api/v1/authz-integration/my-grants", "GET"),
    ("/api/v1/authz-integration/descriptor-keys", "GET"),
    ("/api/v1/authz-integration/descriptor-keys", "POST"),
    ("/api/v1/authz-integration/descriptor-keys/{key_id}", "PATCH"),
    ("/api/v1/authz-integration/descriptor-keys/{key_id}", "DELETE"),
)


def test_blank_backend_login_and_platform_contracts(monkeypatch) -> None:
    from enterprise_platform.rate_limit import reset_rate_limits

    reset_rate_limits()
    assert os.environ.get("BLANK_DATABASE_URL", "").startswith("postgresql")
    from blank_app.main import app

    with TestClient(app) as client:
        # 本用例断言的是 seed 刚建好的初始 admin(必须改密、无 TOTP、无 passkey)。
        # 该状态由自己建立,不能指望「本用例先于任何调用 _reset_admin 的用例运行」。
        # 必须放在进入 TestClient 之后:admin 由 blank_app 的 lifespan 启动时才落库。
        _reset_admin(must_change_password=True)
        _pin_totp_clock(monkeypatch)

        wrong = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
        assert wrong.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
        )
        assert login.status_code == 200
        assert login.json()["mustChangePassword"] is True
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["name"] == "admin"
        assert me.json()["hasLocalPassword"] is True
        assert me.json()["roleGroups"] == []
        assert all(me.json()["securityCapabilities"].values())

        footer = client.get("/api/v1/app-settings/footer")
        assert footer.status_code == 200
        assert footer.json()["footerHtmlEn"] == "Enterprise App · © {year}"

        notifications = client.get("/api/v1/notifications", headers=headers)
        assert notifications.status_code == 403
        assert notifications.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"

        temporary_password = "temporary-password-43!"
        changed = client.post(
            "/api/v1/users/me/password",
            headers=headers,
            json={
                "currentPassword": os.environ["BLANK_ADMIN_PASSWORD"],
                "newPassword": temporary_password,
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

        temporary_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": temporary_password},
        )
        temporary_headers = {"Authorization": f"Bearer {temporary_login.json()['accessToken']}"}
        restored = client.post(
            "/api/v1/users/me/password",
            headers=temporary_headers,
            json={
                "currentPassword": temporary_password,
                "newPassword": os.environ["BLANK_ADMIN_PASSWORD"],
            },
        )
        assert restored.status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
        )
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

        notifications = client.get("/api/v1/notifications", headers=headers)
        assert notifications.status_code == 200
        assert notifications.json() == {"items": [], "unreadCount": 0, "nextCursor": None}

        passkey_begin = client.post("/api/v1/users/me/passkeys/register/begin", headers=headers)
        assert passkey_begin.status_code == 200
        assert passkey_begin.json()["options"]["rp"]["id"] == os.environ["BLANK_WEBAUTHN_RP_ID"]

        totp_begin = client.post("/api/v1/users/me/totp/begin", headers=headers)
        assert totp_begin.status_code == 200
        secret = totp_begin.json()["secret"]
        code = _totp_code(secret)
        assert client.post("/api/v1/users/me/totp/confirm", headers=headers, json={"code": code}).status_code == 200
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

        require_totp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
        )
        assert require_totp.status_code == 401
        assert require_totp.json()["detail"]["code"] == "REQUIRE_SECOND_FACTOR"
        totp_login = client.post(
            "/api/v1/auth/login",
            json={
                "username": "admin",
                "password": os.environ["BLANK_ADMIN_PASSWORD"],
                "totpCode": _totp_code(secret),
            },
        )
        headers = {"Authorization": f"Bearer {totp_login.json()['accessToken']}"}
        disabled = client.post(
            "/api/v1/users/me/totp/disable",
            headers=headers,
            json={
                "password": os.environ["BLANK_ADMIN_PASSWORD"],
                "code": _totp_code(secret),
            },
        )
        assert disabled.status_code == 200
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

        final_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
        )
        headers = {"Authorization": f"Bearer {final_login.json()['accessToken']}"}
        assert client.delete("/api/v1/users/me/passkeys/missing", headers=headers).status_code == 404

        upstream = client.get("/api/v1/ops/upstream-health", headers=headers)
        assert upstream.status_code == 200
        assert {item["dependency"] for item in upstream.json()} == {
            "authentik",
            "easyauth_directory",
            "easyauth",
            "scheduler",
        }
        items = {item["dependency"]: item for item in upstream.json()}
        assert items["easyauth_directory"]["supported"] is True
        unsupported = {item["dependency"] for item in upstream.json() if item["supported"] is False}
        assert unsupported == {"scheduler"}
        checked = client.post("/api/v1/ops/upstream-health/checks", headers=headers)
        assert checked.status_code == 200, checked.text
        directory = next(item for item in checked.json() if item["dependency"] == "easyauth_directory")
        assert directory["supported"] is True
        assert "not_configured" in directory["summary"]
        assert directory["status"] == "unknown"
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

        assert client.get("/health").json() == {"status": "ok", "service": "enterprise-blank"}
    reset_rate_limits()


def test_blank_mounts_shared_authorization_router_once() -> None:
    from fastapi.routing import APIRoute

    import blank_app.authz_api as authz_api
    from blank_app.authz_api import router
    from blank_app.main import app

    for path, method in AUTHORIZATION_ROUTE_CONTRACT:
        shared_path = path.removeprefix("/api/v1")
        matches = [
            route
            for route in router.routes
            if isinstance(route, APIRoute) and route.path == shared_path and method in route.methods
        ]
        assert len(matches) == 1
        assert matches[0].endpoint.__module__ == "enterprise_platform.authorization"
        assert method.lower() in app.openapi()["paths"][path]

    actual = {
        (f"/api/v1{route.path}", method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert actual == set(AUTHORIZATION_ROUTE_CONTRACT)
    assert not hasattr(authz_api, "legacy_router")


def _admin_password() -> str:
    return os.environ["BLANK_ADMIN_PASSWORD"]


def _reset_admin(
    *,
    totp_secret: str | None = None,
    passkey_credential_id: str | None = None,
    must_change_password: bool = False,
) -> str:
    """把 admin 复位成可直接登录的状态并返回 account_id(限流回归的前置条件)。

    ``must_change_password=True`` 复原的是 seed 刚建好、尚未首登改密的初始状态。
    """

    from blank_app.adapters import pwd_context
    from blank_app.database import SessionLocal
    from blank_app.models import Account, Passkey

    with SessionLocal() as db:
        account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        account.password_hash = pwd_context.hash(_admin_password())
        account.must_change_password = must_change_password
        account.sessions_revoked_at = None
        account.totp_secret = totp_secret
        account.totp_pending_secret = None
        account.totp_enabled = totp_secret is not None
        db.query(Passkey).filter(Passkey.account_id == account.id).delete()
        if passkey_credential_id is not None:
            db.add(
                Passkey(
                    account_id=account.id,
                    credential_id=passkey_credential_id,
                    public_key=base64.b64encode(b"fake-public-key").decode(),
                    sign_count=0,
                    name="regression-key",
                )
            )
        db.commit()
        return str(account.id)


def _login(client: TestClient, *, password: str | None = None, totp_code: str | None = None):
    body = {"username": os.environ["BLANK_ADMIN_USERNAME"], "password": password or _admin_password()}
    if totp_code is not None:
        body["totpCode"] = totp_code
    return client.post("/api/v1/auth/login", json=body)


# 服务端校验发生在用例生成验证码之后,中间还隔着一次 bcrypt。30 秒步长边界一旦落进这个
# 间隙,pyotp 真实实现就会判错 —— 这是历史 flake 的根因,实测把服务端校验时刻整体 +30s
# 就能稳定复现同一组失败(包括把 TOTP 留在启用状态、连累后续用例全 401 的级联)。
# 因此这里只钉死时钟:HMAC 推导与常量时间比较仍走 pyotp 真实实现,错误验证码照样失败,
# 秘钥对不上也照样失败;绝不能改成直接桩掉 verify(那等于把断言变成永真)。
TOTP_FROZEN_TIME = datetime(2026, 7, 27, 12, 0, 0)


def _pin_totp_clock(monkeypatch) -> None:
    real_verify = pyotp.TOTP.verify
    monkeypatch.setattr(
        pyotp.TOTP,
        "verify",
        lambda self, code, for_time=None, valid_window=0: real_verify(
            self, code, for_time=TOTP_FROZEN_TIME, valid_window=valid_window
        ),
    )


def _totp_code(secret: str) -> str:
    return pyotp.TOTP(secret).at(TOTP_FROZEN_TIME)


def test_repeated_successful_password_and_totp_logins_never_lock_the_account(monkeypatch) -> None:
    """F-04:成功的密码 / TOTP 登录不消耗账号失败额度,不会把合法用户锁成 429。"""

    from blank_app.main import app
    from enterprise_platform.rate_limit import LOGIN_ACCOUNT_FAILURES, reset_rate_limits

    attempts = LOGIN_ACCOUNT_FAILURES.max_attempts + 3
    with TestClient(app) as client:
        _reset_admin()
        reset_rate_limits()
        assert [_login(client).status_code for _ in range(attempts)] == [200] * attempts

        _pin_totp_clock(monkeypatch)
        secret = pyotp.random_base32()
        stable_code = _totp_code(secret)
        _reset_admin(totp_secret=secret)
        reset_rate_limits()
        codes = [_login(client, totp_code=stable_code).status_code for _ in range(attempts)]
        assert codes == [200] * attempts
    _reset_admin()


def test_multi_step_passkey_login_success_does_not_accumulate_failures(monkeypatch) -> None:
    """F-04:Passkey begin + complete 每轮两次请求,全部成功时不得累计失败额度。"""

    import enterprise_platform.passkeys as shared_passkeys
    from blank_app.main import app
    from enterprise_platform.rate_limit import LOGIN_ACCOUNT_FAILURES, reset_rate_limits

    credential_id = bytes_to_base64url(b"regression-credential")
    _reset_admin(passkey_credential_id=credential_id)
    sign_counts = iter(range(1, 100))
    monkeypatch.setattr(
        shared_passkeys.webauthn,
        "verify_authentication_response",
        lambda **_kwargs: SimpleNamespace(new_sign_count=next(sign_counts)),
    )

    rounds = LOGIN_ACCOUNT_FAILURES.max_attempts + 2
    with TestClient(app) as client:
        reset_rate_limits()
        for _ in range(rounds):
            begin = client.post(
                "/api/v1/auth/login/passkey/begin",
                json={"username": os.environ["BLANK_ADMIN_USERNAME"], "password": _admin_password()},
            )
            assert begin.status_code == 200, begin.text
            complete = client.post(
                "/api/v1/auth/login/passkey/complete",
                json={
                    "username": os.environ["BLANK_ADMIN_USERNAME"],
                    "password": _admin_password(),
                    "stateToken": begin.json()["stateToken"],
                    "credential": {
                        "id": credential_id,
                        "rawId": credential_id,
                        "type": "public-key",
                        "response": {},
                    },
                },
            )
            assert complete.status_code == 200, complete.text
    _reset_admin()


def test_credential_failures_lock_the_account_and_success_resets_the_quota() -> None:
    """F-04:凭据失败达阈值必须锁定;阈值内的一次成功登录清空已累计的失败额度。"""

    from blank_app.main import app
    from enterprise_platform.rate_limit import LOGIN_ACCOUNT_FAILURES, reset_rate_limits

    threshold = LOGIN_ACCOUNT_FAILURES.max_attempts
    with TestClient(app) as client:
        _reset_admin()
        reset_rate_limits()
        assert [_login(client, password="wrong").status_code for _ in range(threshold)] == [401] * threshold
        assert _login(client, password="wrong").status_code == 429
        # 已锁定时正确口令同样被拒,证明锁定生效而非只对错误口令生效。
        assert _login(client).status_code == 429

        reset_rate_limits()
        near_lock = threshold - 1
        assert [_login(client, password="wrong").status_code for _ in range(near_lock)] == [401] * near_lock
        assert _login(client).status_code == 200
        # 成功已 reset 额度:再来 threshold-1 次失败仍不应锁定。
        assert [_login(client, password="wrong").status_code for _ in range(near_lock)] == [401] * near_lock
        assert _login(client).status_code == 200
    _reset_admin()


def test_require_second_factor_prompt_is_not_counted_as_credential_failure(monkeypatch) -> None:
    """F-04:密码正确但缺 TOTP 属于中间态,反复触发不得把合法用户锁死。"""

    from blank_app.main import app
    from enterprise_platform.rate_limit import LOGIN_ACCOUNT_FAILURES, reset_rate_limits

    _pin_totp_clock(monkeypatch)
    secret = pyotp.random_base32()
    _reset_admin(totp_secret=secret)
    attempts = LOGIN_ACCOUNT_FAILURES.max_attempts + 2
    with TestClient(app) as client:
        reset_rate_limits()
        for _ in range(attempts):
            response = _login(client)
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "REQUIRE_SECOND_FACTOR"
        assert _login(client, totp_code=_totp_code(secret)).status_code == 200
    _reset_admin()


def test_second_factor_failures_lock_and_success_resets(monkeypatch) -> None:
    """F-04:TOTP confirm 失败消耗二次验证额度,成功后额度重置。"""

    import enterprise_platform.rate_limit as rate_limit
    from blank_app.main import app
    from enterprise_platform.rate_limit import Window, reset_rate_limits

    threshold = 3
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_ACCOUNT_FAILURES", Window(threshold, 300))
    _pin_totp_clock(monkeypatch)
    with TestClient(app) as client:
        _reset_admin()
        reset_rate_limits()
        headers = {"Authorization": f"Bearer {_login(client).json()['accessToken']}"}

        begin = client.post("/api/v1/users/me/totp/begin", headers=headers)
        assert begin.status_code == 200
        secret = begin.json()["secret"]

        def confirm(code: str) -> int:
            return client.post("/api/v1/users/me/totp/confirm", headers=headers, json={"code": code}).status_code

        assert [confirm("000000") for _ in range(threshold)] == [401] * threshold
        assert confirm("000000") == 429

        reset_rate_limits()
        assert confirm("000000") == 401
        assert confirm(_totp_code(secret)) == 200
    _reset_admin()


@pytest.mark.parametrize("forwarded", ["203.0.113.5", ""])
def test_login_ip_admission_rejects_before_creating_account_buckets(forwarded) -> None:
    """F-04:IP 总速率先判定,被拒时不为伪造用户名建键,内存不随伪造标识增长。"""

    from blank_app.main import app
    from enterprise_platform.rate_limit import LOGIN_IP, reset_rate_limits, tracked_key_count

    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    with TestClient(app) as client:
        reset_rate_limits()
        for index in range(LOGIN_IP.max_attempts):
            response = client.post(
                "/api/v1/auth/login",
                headers=headers,
                json={"username": f"forged-{index}", "password": "wrong"},
            )
            assert response.status_code == 401
        keys_after_flood = tracked_key_count()
        blocked = client.post(
            "/api/v1/auth/login",
            headers=headers,
            json={"username": "forged-after-limit", "password": "wrong"},
        )
        assert blocked.status_code == 429
        # 被 IP 准入拒绝的用户名不会新增账号条目。
        assert tracked_key_count() == keys_after_flood
        reset_rate_limits()


def test_public_get_general_defaults_and_footer_shim() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        general = client.get("/api/v1/app-settings/general")
        footer = client.get("/api/v1/app-settings/footer")
        assert general.status_code == 200
        assert footer.status_code == 200
        payload = general.json()
        assert payload["footerHtmlEn"] == footer.json()["footerHtmlEn"] == "Enterprise App · © {year}"
        assert payload["titleZh"] == payload["titleEn"] == ""
        assert payload["logoDataUrl"] is None
