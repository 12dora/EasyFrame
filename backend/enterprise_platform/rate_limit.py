"""共享本地认证限流:全局/IP 速率准入 + 账号凭据失败计数。

准入顺序固定为「全局 -> IP -> 账号失败额度」。全局与 IP 维度统计的是请求总速率,
账号维度只统计真实凭据失败:登录、二次验证成功后立刻清空账号桶,因此正常用户的成功
登录(含 TOTP、Passkey 多步)不会消耗自己的失败额度。账号桶只在凭据失败时创建,全局或
IP 准入被拒时不会为伪造的用户名建键。

内存计数受 TTL 与总量上限约束,伪造标识无法把进程内存撑爆。单进程实现只覆盖单副本
部署,多副本需换共享存储。
"""

from __future__ import annotations

import os
import threading
import time
from _thread import LockType
from collections import OrderedDict
from dataclasses import dataclass
from types import TracebackType

from fastapi import Request

from enterprise_platform.auth import AuthError

# 全局桶只有一个键;IP / 账号桶按标识分键。
GLOBAL_IDENTIFIER = "*"
# 内存上限与过期清扫参数:超过上限时淘汰最久未更新的键。
MAX_TRACKED_KEYS = 20_000
ENTRY_TTL_SECONDS = 900.0
SWEEP_INTERVAL_SECONDS = 60.0
RATE_LIMIT_DETAIL = "尝试过于频繁, 请稍后再试"


@dataclass(frozen=True)
class Window:
    """滑动窗口:window_seconds 秒内最多允许 max_attempts 次。"""

    max_attempts: int
    window_seconds: float


# 全局/IP 是请求总速率;账号维度是凭据失败额度,两者分开计数。
LOGIN_GLOBAL = Window(600, 60)
LOGIN_IP = Window(30, 300)
LOGIN_ACCOUNT_FAILURES = Window(10, 300)
SECOND_FACTOR_GLOBAL = Window(600, 60)
SECOND_FACTOR_IP = Window(30, 300)
SECOND_FACTOR_ACCOUNT_FAILURES = Window(10, 300)


class BoundedSlidingWindowCounter:
    """线程安全滑动窗口计数;条目按 TTL 过期,总量超上限时淘汰最久未更新的键。"""

    def __init__(self, *, max_keys: int = MAX_TRACKED_KEYS, entry_ttl_seconds: float = ENTRY_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._hits: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._max_keys = max_keys
        self._entry_ttl_seconds = entry_ttl_seconds
        self._last_sweep = time.monotonic()

    def record(self, bucket: str, identifier: str, window: Window) -> int:
        """记录一次尝试并返回窗口内计数(含本次)。"""

        now = time.monotonic()
        key = (bucket, identifier)
        with self._lock:
            self._sweep_expired(now)
            cutoff = now - window.window_seconds
            timestamps = [item for item in self._hits.get(key, ()) if item > cutoff]
            timestamps.append(now)
            self._hits[key] = timestamps
            self._hits.move_to_end(key)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
            return len(timestamps)

    def count(self, bucket: str, identifier: str, window: Window) -> int:
        """只读查询窗口内计数;绝不创建新条目。"""

        cutoff = time.monotonic() - window.window_seconds
        with self._lock:
            return sum(1 for item in self._hits.get((bucket, identifier), ()) if item > cutoff)

    def clear(self, bucket: str, identifier: str) -> None:
        """清空单个标识的计数(成功后重置额度)。"""

        with self._lock:
            self._hits.pop((bucket, identifier), None)

    def reset(self) -> None:
        """清空全部计数(测试夹具用)。"""

        with self._lock:
            self._hits.clear()
            self._last_sweep = time.monotonic()

    def tracked_keys(self) -> int:
        """当前跟踪的键数量,用于验证内存有界。"""

        with self._lock:
            return len(self._hits)

    def _sweep_expired(self, now: float) -> None:
        """按 TTL 批量清理过期条目;调用方必须已持锁。"""

        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - self._entry_ttl_seconds
        expired = [key for key, timestamps in self._hits.items() if not timestamps or timestamps[-1] <= cutoff]
        for key in expired:
            del self._hits[key]


_counter = BoundedSlidingWindowCounter()
# 固定条带锁使同一账号的「检查额度 -> 认证 -> 记录失败」成为原子区间，
# 同时避免按攻击者提供的 username 动态创建无界锁对象。
_ACCOUNT_ADMISSION_LOCKS = tuple(threading.Lock() for _ in range(256))


def reset_rate_limits() -> None:
    """清空限流状态(测试夹具用)。"""

    _counter.reset()


def tracked_key_count() -> int:
    """当前限流器跟踪的键数量。"""

    return _counter.tracked_keys()


def account_identifier(value: str) -> str:
    """账号维度标识归一化,避免大小写/空格绕过。"""

    return value.strip().lower()


class _AccountFailureAdmission:
    """持有账号条带锁的上下文管理器。

    保持类式实现是纵深防御，不是唯一修复：根因(``AuthError`` 曾是 frozen dataclass，
    被 contextlib 的 ``exc.__traceback__ = traceback`` 污染)已在
    ``enterprise_platform/auth.py`` 修掉。类式 ``__exit__`` 不经过 ``gen.throw()``，
    因此即使将来又有人把某个业务异常声明成 frozen，这段临界区也不会把它变形。
    """

    def __init__(self, lock: LockType) -> None:
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        self._lock.release()
        return False


def _failure_admission(bucket: str, identifier: str) -> _AccountFailureAdmission:
    """按「桶 + 归一化标识」选条带锁。

    归一化必须与 ``record_*``/``clear_*``/``_admit`` 用的 ``account_identifier`` 一致，
    否则 ``ABC`` 和 ``abc`` 会拿到不同的锁却写同一个计数键，临界区形同虚设。
    带上 bucket 前缀是为了让登录与二次验证两个命名空间落在不同条带，避免同一主体的
    两类请求无谓地互相排队;它们的计数键本来就是分开的，因此不带前缀也只是性能问题。
    """

    normalized = f"{bucket}:{account_identifier(identifier)}"
    lock = _ACCOUNT_ADMISSION_LOCKS[hash(normalized) % len(_ACCOUNT_ADMISSION_LOCKS)]
    return _AccountFailureAdmission(lock)


def account_failure_admission(identifier: str) -> _AccountFailureAdmission:
    """串行化同一账号的登录失败额度准入与认证结果记录。

    调用方必须把 ``admit_*``、凭据校验、``record_*``/``clear_*`` 全部放在
    context 内，防止一批并发请求在任一失败落账前同时越过额度检查。
    """

    return _failure_admission("login", identifier)


def second_factor_failure_admission(identifier: str) -> _AccountFailureAdmission:
    """串行化同一主体的二次验证失败额度准入与验证结果记录。

    这里的主体是 ``CurrentUser.id`` 而不是用户名，但归一化沿用 ``account_identifier``，
    与 ``record_second_factor_failure`` / ``admit_second_factor`` 的计数键保持同一口径。
    任何调用点都只获取一个条带锁、且不嵌套获取另一个，因此不存在死锁路径。
    """

    return _failure_admission("second-factor", identifier)


def client_ip(request: Request) -> str:
    """取客户端 IP;只有显式信任反代时才采用 X-Forwarded-For 首跳。"""

    if os.getenv("ENTERPRISE_TRUST_FORWARD_HEADERS", "false").lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client and request.client.host else "unknown"


def admit_login(*, ip: str, username: str) -> None:
    """登录准入:全局 -> IP -> 账号失败额度,前两级被拒时不创建账号键。"""

    _admit(
        ip=ip,
        identifier=username,
        bucket="login",
        global_window=LOGIN_GLOBAL,
        ip_window=LOGIN_IP,
        failure_window=LOGIN_ACCOUNT_FAILURES,
    )


def record_login_failure(username: str) -> None:
    """记录一次登录凭据失败;只有真实凭据失败才消耗账号额度。"""

    _counter.record(_failure_bucket("login"), account_identifier(username), LOGIN_ACCOUNT_FAILURES)


def clear_login_failures(username: str) -> None:
    """登录成功后清空账号失败额度,避免合法用户被自己的成功登录锁死。"""

    _counter.clear(_failure_bucket("login"), account_identifier(username))


def admit_second_factor(*, ip: str, identifier: str) -> None:
    """二次验证准入:与登录同样先做全局/IP 准入,再查账号失败额度。"""

    _admit(
        ip=ip,
        identifier=identifier,
        bucket="second-factor",
        global_window=SECOND_FACTOR_GLOBAL,
        ip_window=SECOND_FACTOR_IP,
        failure_window=SECOND_FACTOR_ACCOUNT_FAILURES,
    )


def record_second_factor_failure(identifier: str) -> None:
    """记录一次二次验证失败。"""

    _counter.record(_failure_bucket("second-factor"), account_identifier(identifier), SECOND_FACTOR_ACCOUNT_FAILURES)


def clear_second_factor_failures(identifier: str) -> None:
    """二次验证成功后清空失败额度。"""

    _counter.clear(_failure_bucket("second-factor"), account_identifier(identifier))


def enforce_login(request: Request, username: str) -> None:
    """共享登录入口准入(使用共享 IP 解析策略)。"""

    admit_login(ip=client_ip(request), username=username)


def enforce_second_factor(request: Request, account_id: str) -> None:
    """共享二次验证入口准入(使用共享 IP 解析策略)。"""

    admit_second_factor(ip=client_ip(request), identifier=account_id)


def _failure_bucket(bucket: str) -> str:
    return f"{bucket}:account-failure"


def _admit(
    *,
    ip: str,
    identifier: str,
    bucket: str,
    global_window: Window,
    ip_window: Window,
    failure_window: Window,
) -> None:
    """按「全局 -> IP -> 账号失败额度」顺序准入,任一维度超限抛 429。"""

    if _counter.record(f"{bucket}:global", GLOBAL_IDENTIFIER, global_window) > global_window.max_attempts:
        raise AuthError(429, RATE_LIMIT_DETAIL)
    if _counter.record(f"{bucket}:ip", ip, ip_window) > ip_window.max_attempts:
        raise AuthError(429, RATE_LIMIT_DETAIL)
    # 只读查询:账号键只会在真实凭据失败时被创建。
    if _counter.count(_failure_bucket(bucket), account_identifier(identifier), failure_window) >= (
        failure_window.max_attempts
    ):
        raise AuthError(429, RATE_LIMIT_DETAIL)
