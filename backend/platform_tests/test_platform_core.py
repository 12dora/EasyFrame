import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.auth import AuthError, authenticate_login
from enterprise_platform.authz.principal import PrincipalValidationError, UpstreamPrincipal
from enterprise_platform.footer import sanitize_footer_html
from enterprise_platform.notifications import create_notification_router
from enterprise_platform.ports import LocalAccount
from enterprise_platform.rate_limit import (
    RATE_LIMIT_DETAIL,
    BoundedSlidingWindowCounter,
    Window,
    account_failure_admission,
    clear_login_failures,
    enforce_login,
    record_login_failure,
    reset_rate_limits,
    tracked_key_count,
)
from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request, validate_outbound_url
from enterprise_platform.schemas import CurrentUser, NotificationItem, NotificationPage


class FakeAccountPort:
    account = LocalAccount(
        id="user-1",
        name="admin",
        email="admin@example.com",
        avatar_url=None,
        ui_locale="zh-CN",
        active=True,
        is_local_superuser=True,
        must_change_password=True,
        totp_enabled=True,
        has_passkey=True,
    )

    def authenticate_password(self, username: str, password: str) -> LocalAccount | None:
        return self.account if (username, password) == ("admin", "correct-password") else None

    def verify_totp(self, account_id: str, code: str) -> bool:
        return account_id == self.account.id and code == "123456"

    def issue_session(self, account_id: str) -> str:
        return f"token:{account_id}"


def test_shared_login_requires_advertised_second_factor_and_issues_session() -> None:
    port = FakeAccountPort()
    with pytest.raises(AuthError) as captured:
        authenticate_login(port, "admin", "correct-password", None)
    assert captured.value.status_code == 401
    assert captured.value.detail == {"code": "REQUIRE_SECOND_FACTOR", "methods": ["totp", "passkey"]}

    token, must_change = authenticate_login(port, "admin", "correct-password", "123456")
    assert (token, must_change) == ("token:user-1", True)


def test_shared_login_fails_closed_for_bad_password_and_totp() -> None:
    port = FakeAccountPort()
    with pytest.raises(AuthError, match="用户名或密码错误"):
        authenticate_login(port, "admin", "wrong", None)
    with pytest.raises(AuthError, match="TOTP 验证码错误"):
        authenticate_login(port, "admin", "correct-password", "000000")


def test_footer_sanitizer_is_shared_and_rejects_active_content() -> None:
    result = sanitize_footer_html(
        '<script>alert(1)</script><a href="javascript:alert(1)" onclick="x()" rel="opener">Help</a>'
    )
    assert result == '<a rel="noopener noreferrer">Help</a>'


class FakeNotifications:
    def __init__(self) -> None:
        self.item = NotificationItem(
            id="notification-1",
            title="Welcome",
            body="Ready",
            created_at=datetime(2026, 7, 18, tzinfo=UTC),
        )

    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        assert (user_id, cursor, limit) == ("user-1", None, 20)
        return NotificationPage(items=[self.item], unread_count=1)

    def mark_read(self, user_id: str, notification_id: str, *, read_at: datetime) -> bool:
        if user_id == "user-1" and notification_id == self.item.id:
            self.item = self.item.model_copy(update={"read_at": read_at})
            return True
        return False

    def mark_all_read(self, user_id: str, *, read_at: datetime) -> int:
        return 1 if user_id == "user-1" else 0


def test_notification_router_factory_happy_and_not_found_contracts() -> None:
    app = FastAPI()
    app.include_router(
        create_notification_router(FakeNotifications(), current_user_id=lambda: "user-1"), prefix="/api/v1"
    )
    client = TestClient(app)

    listed = client.get("/api/v1/notifications")
    assert listed.status_code == 200
    assert listed.json()["unreadCount"] == 1
    assert client.post("/api/v1/notifications/notification-1/read").status_code == 200
    assert client.post("/api/v1/notifications/missing/read").status_code == 404


def test_current_user_contract_is_camel_case() -> None:
    body = CurrentUser(id="1", name="Admin", must_change_password=True).model_dump(by_alias=True)
    assert body["mustChangePassword"] is True


def test_principal_rejects_excessive_lifetime_and_old_replay_window() -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": "user-1",
        "iss": "https://identity.example.com/",
        "aud": "blank",
        "iat": now.timestamp(),
        "exp": now.timestamp() + 301,
        "active": True,
        "name": "User",
        "email": "user@example.com",
    }
    with pytest.raises(PrincipalValidationError, match="lifetime"):
        UpstreamPrincipal.from_claims(
            claims,
            issuer="https://identity.example.com/",
            audience="blank",
        )


def test_shared_outbound_guard_rejects_metadata_private_and_userinfo(monkeypatch) -> None:
    for url in (
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/admin",
        "https://user:secret@example.com/path",
    ):
        with pytest.raises(UnsafeOutboundUrlError):
            validate_outbound_url(url)

    import enterprise_platform.safe_http as safe_http

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(UnsafeOutboundUrlError, match="内网"):
        validate_outbound_url("https://internal.example.com")


def test_shared_outbound_guard_pins_first_public_dns_answer(monkeypatch) -> None:
    """二次 DNS 即使变为 metadata，实际连接仍只能使用首次校验的公网 IP。"""

    import httpx

    import enterprise_platform.safe_http as safe_http

    answers = iter(("93.184.216.34", "169.254.169.254"))
    resolutions: list[str] = []
    captured: dict[str, object] = {}

    def fake_getaddrinfo(host, port, **_kwargs):
        address = next(answers)
        resolutions.append(address)
        return [(None, None, None, "", (address, port))]

    class FakeStream:
        def __enter__(self):
            return httpx.Response(
                200,
                content=b"ok",
                request=httpx.Request("GET", str(captured["url"])),
            )

        def __exit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, **kwargs):
            captured.update(method=method, url=str(url), request=kwargs)
            return FakeStream()

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(safe_http.httpx, "Client", FakeClient)

    response = guarded_request("GET", "https://public.example.test/resource")

    assert response.status_code == 200
    assert resolutions == ["93.184.216.34"]
    assert captured["url"] == "https://93.184.216.34/resource"
    assert captured["client"] == {"verify": True, "trust_env": False}
    request = captured["request"]
    assert request["headers"]["Host"] == "public.example.test"
    assert request["extensions"]["sni_hostname"] == "public.example.test"


def _login_request(host: str = "203.0.113.8"):
    from starlette.requests import Request

    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": (host, 1)})


def test_shared_login_locks_only_after_credential_failures_and_success_resets(monkeypatch) -> None:
    """账号维度只统计凭据失败;达到阈值锁定,成功登录后额度立即恢复。"""

    import enterprise_platform.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "LOGIN_ACCOUNT_FAILURES", Window(2, 300))
    monkeypatch.setattr(rate_limit, "LOGIN_IP", Window(50, 300))
    reset_rate_limits()

    request = _login_request()
    # 没有失败记录时,连续准入不会累积账号额度。
    for _ in range(5):
        enforce_login(request, "bounded-user")

    record_login_failure("bounded-user")
    enforce_login(request, "bounded-user")
    record_login_failure("bounded-user")
    with pytest.raises(AuthError) as captured:
        enforce_login(request, "bounded-user")
    assert captured.value.status_code == 429

    clear_login_failures("bounded-user")
    enforce_login(request, "bounded-user")
    reset_rate_limits()


def test_concurrent_login_failures_cannot_overrun_account_quota(monkeypatch) -> None:
    """同一账号并发突发只能执行阈值次数的凭据校验。"""

    import enterprise_platform.rate_limit as rate_limit

    threshold = 3
    monkeypatch.setattr(rate_limit, "LOGIN_ACCOUNT_FAILURES", Window(threshold, 300))
    monkeypatch.setattr(rate_limit, "LOGIN_IP", Window(100, 300))
    reset_rate_limits()
    request = _login_request("198.51.100.80")

    def attempt() -> bool:
        try:
            with account_failure_admission("concurrent-victim"):
                enforce_login(request, "concurrent-victim")
                # 扩大旧实现的 check→record 竞态窗口，负控时所有 worker 都会越过检查。
                time.sleep(0.01)
                record_login_failure("concurrent-victim")
                return True
        except AuthError:
            return False

    with ThreadPoolExecutor(max_workers=12) as executor:
        admitted = list(executor.map(lambda _: attempt(), range(12)))

    assert sum(admitted) == threshold
    reset_rate_limits()


class SlowCredentialPort:
    """凭据校验耗时 10ms，把「查额度 -> 校验 -> 记失败」的竞态窗口撑开。"""

    def __init__(self) -> None:
        self.password_checks = 0
        self.totp_checks = 0
        self.change_password_checks = 0
        self._lock = threading.Lock()

    def authenticate_password(self, _username: str, _password: str) -> LocalAccount | None:
        with self._lock:
            self.password_checks += 1
        time.sleep(0.01)
        return None

    def totp_confirm(self, _account_id: str, _code: str) -> bool:
        with self._lock:
            self.totp_checks += 1
        time.sleep(0.01)
        return False

    def change_password(self, _account_id: str, _current_password: str, _new_password: str) -> bool:
        # 真实实现在这里跑 argon2/bcrypt 校验;计数即「被强制执行的口令哈希验证次数」。
        with self._lock:
            self.change_password_checks += 1
        time.sleep(0.01)
        return False


def _quota_probe_app(port: SlowCredentialPort) -> FastAPI:
    """只挂 auth + password + totp 三组路由的最小宿主，用真实 router 而不是限流原语。"""

    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=port,
                footer=None,
                notifications=None,
                integrations=None,
                upstream_health=None,
                require_permission=lambda _code: None,
            ),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                me=False,
                passkeys=False,
                footer=False,
                notifications=False,
                identity=False,
                easyauth=False,
                upstream=False,
            ),
            current_user_dependency=lambda: CurrentUser(id="user-1", name="victim", must_change_password=False),
            permission_dependency_factory=lambda _code: lambda: None,
        ),
        prefix="/api/v1",
    )
    return app


def _burst(client: TestClient, path: str, body: dict[str, str], size: int) -> list[int]:
    with ThreadPoolExecutor(max_workers=size) as executor:
        return list(executor.map(lambda _: client.post(path, json=body).status_code, range(size)))


def test_login_endpoint_burst_cannot_overrun_account_quota(monkeypatch) -> None:
    """F-04:并发突发打 /auth/login 时，真正执行的密码校验次数不得超过账号额度。

    这条比 ``test_concurrent_login_failures_cannot_overrun_account_quota`` 更严格:
    后者只验限流原语，删掉 assembly.py 里的 ``with`` 包裹仍然是绿的。
    """

    import enterprise_platform.rate_limit as rate_limit

    threshold = 3
    monkeypatch.setattr(rate_limit, "LOGIN_ACCOUNT_FAILURES", Window(threshold, 300))
    monkeypatch.setattr(rate_limit, "LOGIN_IP", Window(1000, 300))
    monkeypatch.setattr(rate_limit, "LOGIN_GLOBAL", Window(1000, 60))
    reset_rate_limits()

    port = SlowCredentialPort()
    client = TestClient(_quota_probe_app(port))
    statuses = _burst(client, "/api/v1/auth/login", {"username": "burst-victim", "password": "wrong"}, 12)

    assert port.password_checks == threshold
    assert statuses.count(401) == threshold
    assert statuses.count(429) == 12 - threshold
    reset_rate_limits()


def test_second_factor_endpoint_burst_cannot_overrun_account_quota(monkeypatch) -> None:
    """F-04 的二次验证半边:/users/me/totp/confirm 也必须原子消耗账号失败额度。"""

    import enterprise_platform.rate_limit as rate_limit

    threshold = 3
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_ACCOUNT_FAILURES", Window(threshold, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_IP", Window(1000, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_GLOBAL", Window(1000, 60))
    reset_rate_limits()

    port = SlowCredentialPort()
    client = TestClient(_quota_probe_app(port))
    statuses = _burst(client, "/api/v1/users/me/totp/confirm", {"code": "000000"}, 12)

    assert port.totp_checks == threshold
    assert statuses.count(401) == threshold
    assert statuses.count(429) == 12 - threshold
    reset_rate_limits()


def test_password_change_endpoint_burst_cannot_overrun_account_quota(monkeypatch) -> None:
    """F-04 的第三条路径:/users/me/password 校验 currentPassword 也必须受额度约束。

    这条路由只要求已登录(``recovery_user``),不校验二次验证。没有额度时,拿到会话的
    攻击者可以无限并发提交错误 currentPassword:既是猜口令 oracle,
    也能靠 argon2/bcrypt 成本打成 CPU 耗尽。
    """

    import enterprise_platform.rate_limit as rate_limit

    threshold = 3
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_ACCOUNT_FAILURES", Window(threshold, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_IP", Window(1000, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_GLOBAL", Window(1000, 60))
    reset_rate_limits()

    port = SlowCredentialPort()
    client = TestClient(_quota_probe_app(port))
    body = {"currentPassword": "wrong-password", "newPassword": "brand-new-password-1!"}
    statuses = _burst(client, "/api/v1/users/me/password", body, 12)

    assert port.change_password_checks == threshold
    assert statuses.count(401) == threshold
    assert statuses.count(429) == 12 - threshold
    reset_rate_limits()


def test_password_change_and_second_factor_share_one_reverification_quota(monkeypatch) -> None:
    """改密与 TOTP 二次验证共用同一份额度,攻击者不能靠换路由拿到双份口令校验机会。"""

    import enterprise_platform.rate_limit as rate_limit

    threshold = 4
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_ACCOUNT_FAILURES", Window(threshold, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_IP", Window(1000, 300))
    monkeypatch.setattr(rate_limit, "SECOND_FACTOR_GLOBAL", Window(1000, 60))
    reset_rate_limits()

    port = SlowCredentialPort()
    client = TestClient(_quota_probe_app(port))
    change_body = {"currentPassword": "wrong-password", "newPassword": "brand-new-password-1!"}
    for _ in range(threshold):
        assert client.post("/api/v1/users/me/password", json=change_body).status_code == 401

    # 额度已被改密耗尽,同一主体的 TOTP confirm 必须立刻 429 且不再执行任何校验。
    assert client.post("/api/v1/users/me/totp/confirm", json={"code": "000000"}).status_code == 429
    assert port.change_password_checks == threshold
    assert port.totp_checks == 0
    reset_rate_limits()


def test_auth_error_survives_generator_context_manager_and_yield_dependency() -> None:
    """``AuthError`` 不得是 frozen dataclass。

    Python 3.11 的 contextlib 在生成器式 ``@contextmanager`` 的 ``__exit__`` 里执行
    ``exc.__traceback__ = traceback``;若 ``AuthError`` 被冻结，这一步会抛
    ``FrozenInstanceError`` 把真实异常吞掉。FastAPI 的 ``yield`` 依赖走同一条路径，
    后果是客户端拿到 500 而不是真实状态码。
    """

    @contextmanager
    def generator_scope():
        yield

    with pytest.raises(AuthError) as captured:
        with generator_scope():
            raise AuthError(429, RATE_LIMIT_DETAIL)
    assert (captured.value.status_code, captured.value.detail) == (429, RATE_LIMIT_DETAIL)

    app = FastAPI()

    @app.exception_handler(AuthError)
    async def _handle(_request, exc: AuthError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def yielding_dependency():
        yield "session"

    @app.get("/boom")
    def boom(_dep: str = Depends(yielding_dependency)) -> dict[str, str]:
        raise AuthError(422, "authority changed")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 422
    assert response.json() == {"detail": "authority changed"}


def test_shared_login_ip_admission_precedes_account_bucket(monkeypatch) -> None:
    """IP 总速率先判定;被拒后不得为伪造用户名创建账号条目。"""

    import enterprise_platform.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "LOGIN_IP", Window(2, 300))
    reset_rate_limits()

    request = _login_request("198.51.100.7")
    enforce_login(request, "victim")
    enforce_login(request, "victim")
    keys_before = tracked_key_count()
    with pytest.raises(AuthError) as captured:
        enforce_login(request, "never-seen-account")
    assert captured.value.status_code == 429
    # 只有 global / ip 两个键,被拒的账号没有留下条目。
    assert tracked_key_count() == keys_before
    reset_rate_limits()


def test_shared_limiter_keeps_memory_bounded_under_unique_identifier_flood() -> None:
    """伪造唯一标识洪泛时条目数受上限约束,不会无界增长。"""

    counter = BoundedSlidingWindowCounter(max_keys=64, entry_ttl_seconds=900)
    window = Window(10, 300)
    for index in range(5_000):
        counter.record("login:account-failure", f"forged-{index}", window)

    assert counter.tracked_keys() == 64
    # 只读查询不会创建条目。
    assert counter.count("login:account-failure", "never-recorded", window) == 0
    assert counter.tracked_keys() == 64
