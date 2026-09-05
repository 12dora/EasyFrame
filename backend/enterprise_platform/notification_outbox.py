"""通知 outbox 状态机与发送/对账驱动。

共享内核只描述条目、端口与纯函数;宿主实现 ``NotificationOutboxPort``
(典型为 ``SELECT ... FOR UPDATE SKIP LOCKED``),不得把 ORM 模型泄漏进本模块。

发送端通过 ``NotifySender`` 结构协议对接 F1 的 ``NotifyClient``,本模块不导入
``easyauth.notify``,避免双向耦合。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from enterprise_platform.easyauth.errors import (
    NotifyDedupConflictError,
    NotifyError,
    NotifyRejectedError,
    NotifyThrottledError,
    NotifyUnavailableError,
)

STATUS_QUEUED = "queued"
STATUS_ACCEPTED = "accepted"
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

OUTBOX_STATUSES = frozenset(
    {
        STATUS_QUEUED,
        STATUS_ACCEPTED,
        STATUS_SENT,
        STATUS_DELIVERED,
        STATUS_FAILED,
        STATUS_SUPERSEDED,
    }
)

PROVIDER_RECIPIENT_PENDING = "pending"
PROVIDER_RECIPIENT_THROTTLED = "throttled"
PROVIDER_RECIPIENT_SENT = "sent"
PROVIDER_RECIPIENT_DELIVERED = "delivered"
PROVIDER_RECIPIENT_FAILED = "failed"

ERROR_DEDUP_CONFLICT = "dedup_conflict"
ERROR_DEDUP_KEY_MISMATCH = "dedup_key_mismatch"
ERROR_REJECTED = "rejected"
ERROR_THROTTLED = "throttled"
ERROR_UNAVAILABLE = "unavailable"
ERROR_EXHAUSTED = "exhausted"
ERROR_PROVIDER_MESSAGE_MISSING = "provider_message_missing"

DEFAULT_PROCESS_LIMIT = 50
DEFAULT_LEASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 12
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30 * 60
RECONCILE_HORIZON = timedelta(hours=24)

SendDecision = Literal["retry", "fail"]

_PROVIDER_RECIPIENT_TO_LOCAL = {
    PROVIDER_RECIPIENT_PENDING: STATUS_ACCEPTED,
    PROVIDER_RECIPIENT_THROTTLED: STATUS_ACCEPTED,
    PROVIDER_RECIPIENT_SENT: STATUS_SENT,
    PROVIDER_RECIPIENT_DELIVERED: STATUS_DELIVERED,
    PROVIDER_RECIPIENT_FAILED: STATUS_FAILED,
}

_LOCAL_RECIPIENT_STATUSES = frozenset({STATUS_ACCEPTED, STATUS_SENT, STATUS_DELIVERED, STATUS_FAILED})

_TERMINAL_OUTBOX_STATUSES = frozenset({STATUS_DELIVERED, STATUS_FAILED})

_STATUS_PROGRESS = {
    STATUS_ACCEPTED: 0,
    STATUS_SENT: 1,
    STATUS_DELIVERED: 2,
    STATUS_FAILED: 2,
}


class StaleLeaseError(Exception):
    """租约令牌已与行上的值不一致;调用方应视为他人已接管,不得再写该行。"""


class OutboxNotifyPayload(Protocol):
    """F1 ``NotifyRequest`` 的结构子集。

    宿主必须把同一 ``dedup_key`` 同时写入 ``OutboxItem.dedup_key`` 与
    ``payload.dedup_key``;``process_outbox_once`` 在两者不一致时拒绝发送。
    """

    dedup_key: str | None


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """outbox 工作条目。``payload`` 即 F1 ``NotifyRequest``,此处用结构对象避免导入。"""

    id: str
    dedup_key: str
    payload: OutboxNotifyPayload
    status: str
    attempt_count: int
    next_attempt_at: datetime | None
    provider_message_id: str | None
    last_error: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_reconciled_at: datetime | None
    lease_token: str | None


@dataclass(frozen=True, slots=True)
class AggregatedLocalStatus:
    """按收件人本地状态汇总后的条目状态;``partial`` 表示成功与失败并存。"""

    status: str
    partial: bool = False


@dataclass(frozen=True, slots=True)
class ProcessSummary:
    claimed: int
    accepted: int
    retried: int
    failed: int


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    claimed: int
    applied: int


class NotifySendResultView(Protocol):
    """``NotifyClient.send`` 成功返回值的结构子集。"""

    message_id: str
    accepted: bool
    status: str


class NotifyRecipientStatusView(Protocol):
    """``NotifyRecipientStatus`` 的结构子集。"""

    status: str


class NotifyMessageStatusView(Protocol):
    """``NotifyMessageStatus`` 的结构子集。"""

    status: str
    recipients: Sequence[NotifyRecipientStatusView]


class NotifySender(Protocol):
    """发送/查询协议。F1 ``NotifyClient`` 同名方法即可满足,无需继承。"""

    def send(self, request: OutboxNotifyPayload) -> NotifySendResultView: ...

    def get_message(self, message_id: str) -> NotifyMessageStatusView: ...


class NotificationOutboxPort(Protocol):
    """宿主持久化边界。认领语义由宿主用行锁实现,内核只约定方法。

    SQL 宿主必须把租约令牌写进行,并用它做每次回写的栅栏,内核假定:

    - ``claim_due`` / ``claim_reconcile_due`` 一次原子认领:
      ``UPDATE ... SET lease_token=?, lease_owner=?, lease_expires_at=?
      WHERE (lease_expires_at IS NULL OR lease_expires_at < :now)
      RETURNING ...`` 并配合 ``FOR UPDATE SKIP LOCKED``。
      ``lease_token`` 由端口在认领时生成,随 ``OutboxItem`` 返回。
    - 每一次 ``mark_accepted`` / ``mark_retry`` / ``mark_failed`` /
      ``mark_reconcile_error`` / ``apply_provider_status`` / ``release``:
      ``UPDATE ... WHERE id=:id AND lease_token=:lease_token``。
      影响行数为 0 时必须 no-op:返回 ``False`` 或抛 ``StaleLeaseError``。
      驱动把陈旧结果当作「他人已接管」,不再对该条目继续写。
    - ``apply_provider_status`` 必须单调:不得把 delivered/failed 回写成
      sent/accepted;乱序观察只更新 ``last_reconciled_at``。
    """

    def claim_due(self, limit: int, now: datetime, lease_seconds: int, owner: str) -> list[OutboxItem]: ...

    def mark_accepted(self, id: str, message_id: str, now: datetime, lease_token: str) -> bool: ...

    def mark_retry(self, id: str, next_attempt_at: datetime, error: str, lease_token: str) -> bool: ...

    def mark_failed(self, id: str, error: str, now: datetime, lease_token: str) -> bool: ...

    def claim_reconcile_due(self, limit: int, now: datetime, lease_seconds: int, owner: str) -> list[OutboxItem]: ...

    def apply_provider_status(
        self, id: str, status: NotifyMessageStatusView, now: datetime, lease_token: str
    ) -> bool: ...

    def mark_reconcile_error(self, id: str, error: str, now: datetime, lease_token: str) -> bool: ...

    def release(self, id: str, lease_token: str) -> bool: ...


def backoff_delay(
    attempt: int,
    *,
    jitter: Callable[[float], float] | None = None,
    base_seconds: float = BACKOFF_BASE_SECONDS,
    cap_seconds: float = BACKOFF_CAP_SECONDS,
) -> timedelta:
    """指数退避,上限 30 分钟。

    ``jitter`` 接收已封顶的秒数并返回实际延迟;测试注入恒等函数即可去掉随机。
    缺省为 ``[0.5, 1.0)`` 比例抖动,避免多副本同时打满。
    """

    if attempt < 1:
        raise ValueError("attempt 必须从 1 起算")
    if base_seconds <= 0 or cap_seconds <= 0:
        raise ValueError("退避基数与上限必须为正")
    capped = min(base_seconds * (2 ** (attempt - 1)), cap_seconds)
    apply = jitter if jitter is not None else _default_jitter
    delayed = apply(capped)
    if delayed < 0:
        raise ValueError("jitter 不得返回负数")
    return timedelta(seconds=delayed)


def _default_jitter(delay_seconds: float) -> float:
    return delay_seconds * (0.5 + random.random() * 0.5)


def classify_send_error(exc: BaseException) -> SendDecision:
    """将 F1 抛出的通知异常分为可重试或永久失败;未知类型直接失败,不得吞掉。"""

    if isinstance(exc, (NotifyThrottledError, NotifyUnavailableError)):
        return "retry"
    if isinstance(exc, (NotifyDedupConflictError, NotifyRejectedError)):
        return "fail"
    raise TypeError(f"未分类的发送错误: {type(exc).__name__}") from exc


def map_provider_recipient_status(provider_status: str) -> str:
    """上游收件人状态 → 本地 outbox 状态。未知值立即失败。"""

    try:
        return _PROVIDER_RECIPIENT_TO_LOCAL[provider_status]
    except KeyError:
        raise ValueError(f"未知的上游收件人状态: {provider_status!r}") from None


def aggregate_local_status(recipient_statuses: Sequence[str]) -> AggregatedLocalStatus:
    """按收件人本地状态汇总条目。

    全部 delivered → delivered;全部 failed → failed;任一 sent/delivered → sent;
    成功与失败并存时 ``partial=True``。pending/throttled 已映射为 accepted。
    """

    if not recipient_statuses:
        raise ValueError("收件人状态不能为空")
    unknown = [item for item in recipient_statuses if item not in _LOCAL_RECIPIENT_STATUSES]
    if unknown:
        raise ValueError(f"未知的本地收件人状态: {unknown[0]!r}")

    all_delivered = all(item == STATUS_DELIVERED for item in recipient_statuses)
    all_failed = all(item == STATUS_FAILED for item in recipient_statuses)
    if all_delivered:
        return AggregatedLocalStatus(STATUS_DELIVERED)
    if all_failed:
        return AggregatedLocalStatus(STATUS_FAILED)

    has_success = any(item in (STATUS_SENT, STATUS_DELIVERED) for item in recipient_statuses)
    has_failed = any(item == STATUS_FAILED for item in recipient_statuses)
    if has_success:
        return AggregatedLocalStatus(STATUS_SENT, partial=has_failed)
    return AggregatedLocalStatus(STATUS_ACCEPTED, partial=has_failed)


def clamp_outbox_status(current: str, incoming: str) -> str:
    """单调合并上游观察:不得把 delivered/failed 回写成 sent/accepted。"""

    if current in _TERMINAL_OUTBOX_STATUSES:
        return current
    if _STATUS_PROGRESS.get(incoming, -1) < _STATUS_PROGRESS.get(current, -1):
        return current
    return incoming


def sent_reconcile_expired(sent_at: datetime, now: datetime) -> bool:
    """``sent`` 满 24h 后不再进入对账队列,也不得推断为 delivered。"""

    _require_aware(sent_at, name="sent_at")
    _require_aware(now, name="now")
    return now - sent_at >= RECONCILE_HORIZON


def process_outbox_once(
    port: NotificationOutboxPort,
    client: NotifySender,
    *,
    now: datetime,
    owner: str,
    limit: int = DEFAULT_PROCESS_LIMIT,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    jitter: Callable[[float], float] | None = None,
) -> ProcessSummary:
    """认领到期条目并发送一次。202 与 200 ``accepted:false`` 都记为 accepted。

    发送前校验 ``payload.dedup_key == item.dedup_key``;不一致则
    ``mark_failed(dedup_key_mismatch)`` 且不 POST。
    """

    _require_aware(now, name="now")
    if not owner:
        raise ValueError("owner 不能为空")
    if limit < 1:
        raise ValueError("limit 必须为正")
    if lease_seconds < 1:
        raise ValueError("lease_seconds 必须为正")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须为正")

    claimed = port.claim_due(limit, now, lease_seconds, owner)
    accepted = 0
    retried = 0
    failed = 0
    for item in claimed:
        token = _require_lease_token(item)
        if item.payload.dedup_key != item.dedup_key:
            if _port_write(port.mark_failed, item.id, ERROR_DEDUP_KEY_MISMATCH, now, token):
                failed += 1
            continue
        try:
            result = client.send(item.payload)
        except Exception as exc:
            outcome = _handle_send_failure(
                port, item, exc, now=now, max_attempts=max_attempts, jitter=jitter, lease_token=token
            )
            if outcome == "fail":
                failed += 1
            elif outcome == "retry":
                retried += 1
            continue
        message_id = result.message_id
        if not message_id:
            _port_write(port.release, item.id, token)
            raise ValueError(f"NotifySender.send 必须返回 message_id,条目 {item.id}")
        if _port_write(port.mark_accepted, item.id, message_id, now, token):
            accepted += 1
    return ProcessSummary(claimed=len(claimed), accepted=accepted, retried=retried, failed=failed)


def reconcile_outbox_once(
    port: NotificationOutboxPort,
    client: NotifySender,
    *,
    now: datetime,
    owner: str,
    limit: int = DEFAULT_PROCESS_LIMIT,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ReconcileSummary:
    """轮询上游消息状态并写回。对账成功时必须推进 ``last_reconciled_at``。

    逐条捕获异常:普通错误走 ``mark_reconcile_error`` 后释放,继续其余条目;
    上游消息不存在(F1 无 ``NotifyNotFoundError``,GET 404 为
    ``NotifyRejectedError.status == 404``)则 ``mark_failed(provider_message_missing)``。
    """

    _require_aware(now, name="now")
    if not owner:
        raise ValueError("owner 不能为空")
    if limit < 1:
        raise ValueError("limit 必须为正")
    if lease_seconds < 1:
        raise ValueError("lease_seconds 必须为正")

    claimed = port.claim_reconcile_due(limit, now, lease_seconds, owner)
    applied = 0
    for item in claimed:
        token = _require_lease_token(item)
        try:
            if not item.provider_message_id:
                raise ValueError(f"待对账条目 {item.id} 缺少 provider_message_id")
            message = client.get_message(item.provider_message_id)
        except Exception as exc:
            if _is_provider_message_missing(exc):
                _port_write(port.mark_failed, item.id, ERROR_PROVIDER_MESSAGE_MISSING, now, token)
            else:
                _port_write(port.mark_reconcile_error, item.id, _reconcile_error_text(exc), now, token)
            _port_write(port.release, item.id, token)
            continue
        if _port_write(port.apply_provider_status, item.id, message, now, token):
            applied += 1
        _port_write(port.release, item.id, token)
    return ReconcileSummary(claimed=len(claimed), applied=applied)


def _handle_send_failure(
    port: NotificationOutboxPort,
    item: OutboxItem,
    exc: BaseException,
    *,
    now: datetime,
    max_attempts: int,
    jitter: Callable[[float], float] | None,
    lease_token: str,
) -> SendDecision | None:
    decision = classify_send_error(exc)
    if decision == "fail":
        if _port_write(port.mark_failed, item.id, _fail_error_code(exc), now, lease_token):
            return "fail"
        return None
    next_attempt = item.attempt_count + 1
    if next_attempt >= max_attempts:
        if _port_write(port.mark_failed, item.id, ERROR_EXHAUSTED, now, lease_token):
            return "fail"
        return None
    next_at = _next_retry_at(now, next_attempt, exc, jitter)
    if _port_write(port.mark_retry, item.id, next_at, _retry_error_code(exc), lease_token):
        return "retry"
    return None


def _fail_error_code(exc: BaseException) -> str:
    if isinstance(exc, NotifyDedupConflictError):
        return ERROR_DEDUP_CONFLICT
    if isinstance(exc, NotifyRejectedError):
        return ERROR_REJECTED
    raise TypeError(f"永久失败缺少错误码: {type(exc).__name__}") from exc


def _retry_error_code(exc: BaseException) -> str:
    if isinstance(exc, NotifyThrottledError):
        return ERROR_THROTTLED
    if isinstance(exc, NotifyUnavailableError):
        return ERROR_UNAVAILABLE
    raise TypeError(f"可重试失败缺少错误码: {type(exc).__name__}") from exc


def _next_retry_at(
    now: datetime,
    attempt: int,
    exc: BaseException,
    jitter: Callable[[float], float] | None,
) -> datetime:
    if isinstance(exc, NotifyThrottledError) and exc.retry_after is not None:
        return now + timedelta(seconds=exc.retry_after)
    if not isinstance(exc, NotifyError):
        raise TypeError(f"不可用于计算重试时间: {type(exc).__name__}") from exc
    return now + backoff_delay(attempt, jitter=jitter)


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} 必须带时区")


def _require_lease_token(item: OutboxItem) -> str:
    if not item.lease_token:
        raise ValueError(f"claim 必须返回 lease_token,条目 {item.id}")
    return item.lease_token


def _port_write(action: Callable[..., bool | None], *args: object) -> bool:
    """执行一次带租约的写入;陈旧租约视为他人已接管,不再继续写。"""

    try:
        result = action(*args)
    except StaleLeaseError:
        return False
    return result is not False


def _is_provider_message_missing(exc: BaseException) -> bool:
    """F1 未暴露 ``NotifyNotFoundError``;GET 404 映射为带 ``status`` 的 ``NotifyRejectedError``。"""

    return isinstance(exc, NotifyRejectedError) and getattr(exc, "status", None) == 404


def _reconcile_error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__
