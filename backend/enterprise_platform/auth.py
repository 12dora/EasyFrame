"""共享本地账户登录与二次验证编排。"""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from enterprise_platform.ports import AccountPort, LocalAccount, PasskeyChallenge

REQUIRE_SECOND_FACTOR_CODE = "REQUIRE_SECOND_FACTOR"

# bootstrap 级机密的公共示例标记与弱值黑名单唯一实现:超管口令(seed/重置/自助改密)
# 与签名密钥门禁共用,宿主不得另行维护副本(06 §2)。
PUBLIC_SECRET_MARKERS = ("replace-with-", "change-before-deploy", "changeme")
_WEAK_BOOTSTRAP_VALUES = {"admin123", "password", "secret"}


def is_unsafe_bootstrap_secret(value: str, *, min_length: int) -> bool:
    lowered = value.strip().lower()
    return (
        len(value) < min_length
        or lowered in _WEAK_BOOTSTRAP_VALUES
        or any(marker in lowered for marker in PUBLIC_SECRET_MARKERS)
    )


# 异常类不能用 frozen=True:Python 3.11 的 contextlib 在把异常穿过生成器式
# @contextmanager 时会执行 `exc.__traceback__ = traceback`(contextlib.py:167/179/191),
# frozen dataclass 的 __setattr__ 会把它变成 FrozenInstanceError,原始 AuthError 被吞掉。
# 实测后果:带 `yield` 依赖的路由(app/infra/db/session.py:21 就是)抛 AuthError 时
# 客户端收到 500 而不是真实状态码。
# 用 eq=False 而不是裸 @dataclass:裸 @dataclass 会生成 __eq__ 并把 __hash__ 置 None,
# 使异常不可哈希;eq=False 保留 Exception 的身份语义 __eq__/__hash__。
@dataclass(eq=False)
class AuthError(Exception):
    status_code: int
    detail: Any
    kind: str = "credential"


def is_credential_failure(error: AuthError) -> bool:
    """判断登录失败是否属于凭据错误;「需要二次验证」是中间态,不消耗失败额度。"""

    detail = error.detail
    if isinstance(detail, dict) and detail.get("code") == REQUIRE_SECOND_FACTOR_CODE:
        return False
    return error.status_code == 401 and error.kind == "credential"


def authenticate_login(
    port: AccountPort,
    username: str,
    password: str,
    totp_code: str | None,
    *,
    before_second_factor: Callable[[str], AbstractContextManager[None] | None] | None = None,
) -> tuple[str, bool]:
    """密码 + 可选 TOTP 登录。

    before_second_factor: 密码通过、进入二次验证之前以 account.id 回调一次；
    宿主可返回覆盖整个验证阶段的上下文管理器，以原子执行准入与失败记账。
    返回 None 及不传 callback 均保持旧签名行为。
    """

    account = require_password(port, username, password)
    methods = second_factor_methods(account)
    if methods:
        admission = before_second_factor(account.id) if before_second_factor is not None else None
        with admission or nullcontext():
            if not totp_code or "totp" not in methods:
                raise AuthError(
                    401,
                    {"code": REQUIRE_SECOND_FACTOR_CODE, "methods": methods},
                    kind="second_factor_challenge",
                )
            if not port.verify_totp(account.id, totp_code):
                raise AuthError(401, "TOTP 验证码错误", kind="second_factor")
    return port.issue_session(account.id), account.must_change_password


def require_password(port: AccountPort, username: str, password: str) -> LocalAccount:
    account = port.authenticate_password(username, password)
    if account is None or not account.active:
        raise AuthError(401, "用户名或密码错误")
    return account


def second_factor_methods(account: LocalAccount) -> list[str]:
    return second_factor_methods_for_flags(totp_enabled=account.totp_enabled, has_passkey=account.has_passkey)


def second_factor_methods_for_flags(*, totp_enabled: bool, has_passkey: bool) -> list[str]:
    methods: list[str] = []
    if totp_enabled:
        methods.append("totp")
    if has_passkey:
        methods.append("passkey")
    return methods


def begin_passkey_login(port: AccountPort, username: str, password: str) -> tuple[LocalAccount, PasskeyChallenge]:
    account = require_password(port, username, password)
    if not account.has_passkey:
        raise AuthError(400, "该账户未注册通行密钥")
    return account, port.begin_passkey_login(account.id)


def complete_passkey_login(
    port: AccountPort,
    username: str,
    password: str,
    state_token: str,
    credential: dict[str, Any],
    *,
    before_second_factor: Callable[[str], AbstractContextManager[None] | None] | None = None,
) -> tuple[str, bool]:
    """完成通行密钥登录并签发会话。

    会话只在 ``complete_passkey_login`` 返回 True 之后签发。宿主端口必须在
    **同一事务**内原子消费挑战并更新 ``sign_count``;重放/并发完成不得返回 True。
    """

    account = require_password(port, username, password)
    admission = before_second_factor(account.id) if before_second_factor is not None else None
    with admission or nullcontext():
        try:
            verified = port.complete_passkey_login(account.id, state_token, credential)
        except AuthError as exc:
            # 密码已经通过，此后的宿主验证错误统一属于结构化二次验证失败。
            raise AuthError(exc.status_code, exc.detail, kind="second_factor") from exc
        # 仅当宿主事务成功消费挑战并验证断言后才签发会话,杜绝挑战重放多会话。
        if not verified:
            raise AuthError(401, "通行密钥验证失败", kind="second_factor")
    return port.issue_session(account.id), account.must_change_password
