"""通知 outbox 状态机:内存端口 + 假发送端,无数据库、无网络。"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_platform.easyauth.errors import (
    NotifyDedupConflictError,
    NotifyRejectedError,
    NotifyThrottledError,
    NotifyUnavailableError,
)
from enterprise_platform.notification_outbox import (
    DEFAULT_MAX_ATTEMPTS,
    ERROR_DEDUP_CONFLICT,
    ERROR_DEDUP_KEY_MISMATCH,
    ERROR_EXHAUSTED,
    ERROR_PROVIDER_MESSAGE_MISSING,
    ERROR_REJECTED,
    ERROR_THROTTLED,
    ERROR_UNAVAILABLE,
    RECONCILE_HORIZON,
    STATUS_ACCEPTED,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SENT,
    STATUS_SUPERSEDED,
    AggregatedLocalStatus,
    OutboxItem,
    ProcessSummary,
    aggregate_local_status,
    backoff_delay,
    clamp_outbox_status,
    classify_send_error,
    map_provider_recipient_status,
    process_outbox_once,
    reconcile_outbox_once,
    sent_reconcile_expired,
)
from enterprise_platform.schemas import NotificationDeliveryStatus

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FakeNotifyRequest:
    dedup_key: str
    content: str = "hello"


@dataclass(frozen=True, slots=True)
class FakeSendResult:
    message_id: str
    accepted: bool
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class FakeRecipient:
    status: str
    raw_ref: str = "dt:v1:demo"


@dataclass(frozen=True, slots=True)
class FakeMessageStatus:
    status: str
    recipients: tuple[FakeRecipient, ...]


@dataclass
class _Record:
    item: OutboxItem
    sent_at: datetime | None = None


class InMemoryOutbox:
    """覆盖认领/租约/对账窗口的宿主端口替身。"""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}

    def put(self, item: OutboxItem, *, sent_at: datetime | None = None) -> OutboxItem:
        if item.status == STATUS_SENT and sent_at is None:
            raise ValueError("sent 条目必须提供 sent_at,以便判定 24h 对账窗口")
        self._records[item.id] = _Record(item=item, sent_at=sent_at)
        return item

    def get(self, item_id: str) -> OutboxItem:
        return self._records[item_id].item

    def claim_due(self, limit: int, now: datetime, lease_seconds: int, owner: str) -> list[OutboxItem]:
        claimed: list[OutboxItem] = []
        for record in self._due_queued(now):
            if len(claimed) >= limit:
                break
            item = replace(
                record.item,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                lease_token=secrets.token_urlsafe(12),
            )
            record.item = item
            claimed.append(item)
        return claimed

    def mark_accepted(self, id: str, message_id: str, now: datetime, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        record.item = replace(
            record.item,
            status=STATUS_ACCEPTED,
            provider_message_id=message_id,
            last_error=None,
            lease_owner=None,
            lease_expires_at=None,
            lease_token=None,
        )
        return True

    def mark_retry(self, id: str, next_attempt_at: datetime, error: str, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        record.item = replace(
            record.item,
            attempt_count=record.item.attempt_count + 1,
            next_attempt_at=next_attempt_at,
            last_error=error,
            lease_owner=None,
            lease_expires_at=None,
            lease_token=None,
        )
        return True

    def mark_failed(self, id: str, error: str, now: datetime, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        record.item = replace(
            record.item,
            status=STATUS_FAILED,
            attempt_count=record.item.attempt_count + 1,
            last_error=error,
            lease_owner=None,
            lease_expires_at=None,
            lease_token=None,
        )
        return True

    def claim_reconcile_due(self, limit: int, now: datetime, lease_seconds: int, owner: str) -> list[OutboxItem]:
        due: list[_Record] = []
        for record in self._records.values():
            item = record.item
            if item.status not in (STATUS_ACCEPTED, STATUS_SENT):
                continue
            if item.lease_expires_at is not None and item.lease_expires_at > now:
                continue
            if not item.provider_message_id:
                raise ValueError(f"待对账条目 {item.id} 缺少 provider_message_id")
            if item.status == STATUS_SENT:
                if record.sent_at is None:
                    raise ValueError(f"sent 条目 {item.id} 缺少 sent_at")
                if sent_reconcile_expired(record.sent_at, now):
                    continue
            due.append(record)
        due.sort(key=lambda record: record.item.last_reconciled_at or datetime.min.replace(tzinfo=UTC))
        claimed: list[OutboxItem] = []
        for record in due[:limit]:
            item = replace(
                record.item,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                lease_token=secrets.token_urlsafe(12),
            )
            record.item = item
            claimed.append(item)
        return claimed

    def apply_provider_status(self, id: str, status: FakeMessageStatus, now: datetime, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        mapped = [map_provider_recipient_status(recipient.status) for recipient in status.recipients]
        aggregated = aggregate_local_status(mapped)
        next_status = clamp_outbox_status(record.item.status, aggregated.status)
        sent_at = record.sent_at
        if next_status in (STATUS_SENT, STATUS_DELIVERED) and sent_at is None:
            sent_at = now
        record.sent_at = sent_at
        record.item = replace(
            record.item,
            status=next_status,
            last_reconciled_at=now,
        )
        return True

    def mark_reconcile_error(self, id: str, error: str, now: datetime, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        record.item = replace(record.item, last_error=error, last_reconciled_at=now)
        return True

    def release(self, id: str, lease_token: str) -> bool:
        record = self._leased(id, lease_token)
        if record is None:
            return False
        record.item = replace(record.item, lease_owner=None, lease_expires_at=None, lease_token=None)
        return True

    def _leased(self, item_id: str, lease_token: str) -> _Record | None:
        record = self._require(item_id)
        if not lease_token or record.item.lease_token != lease_token:
            return None
        return record

    def _require(self, item_id: str) -> _Record:
        try:
            return self._records[item_id]
        except KeyError:
            raise KeyError(f"outbox 条目不存在: {item_id}") from None

    def _due_queued(self, now: datetime) -> list[_Record]:
        due: list[_Record] = []
        for record in self._records.values():
            item = record.item
            if item.status != STATUS_QUEUED:
                continue
            if item.next_attempt_at is not None and item.next_attempt_at > now:
                continue
            if item.lease_expires_at is not None and item.lease_expires_at > now:
                continue
            due.append(record)
        due.sort(key=lambda record: (record.item.next_attempt_at or datetime.min.replace(tzinfo=UTC), record.item.id))
        return due


class FakeSender:
    def __init__(
        self,
        *,
        send: Callable[[object], FakeSendResult] | BaseException | FakeSendResult | None = None,
        messages: dict[str, FakeMessageStatus] | None = None,
    ) -> None:
        self._send = send
        self.messages = messages or {}
        self.send_calls: list[object] = []
        self.get_calls: list[str] = []

    def send(self, request: object) -> FakeSendResult:
        self.send_calls.append(request)
        action = self._send
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request)
        if isinstance(action, FakeSendResult):
            return action
        raise AssertionError("FakeSender 未配置 send")

    def get_message(self, message_id: str) -> FakeMessageStatus:
        self.get_calls.append(message_id)
        try:
            return self.messages[message_id]
        except KeyError:
            raise KeyError(f"未知 message_id: {message_id}") from None


def _queued(item_id: str = "item-1", *, dedup_key: str = "dedup-1") -> OutboxItem:
    return OutboxItem(
        id=item_id,
        dedup_key=dedup_key,
        payload=FakeNotifyRequest(dedup_key=dedup_key),
        status=STATUS_QUEUED,
        attempt_count=0,
        next_attempt_at=None,
        provider_message_id=None,
        last_error=None,
        lease_owner=None,
        lease_expires_at=None,
        last_reconciled_at=None,
        lease_token=None,
    )


def _process(port: InMemoryOutbox, sender: FakeSender, *, now: datetime = NOW, owner: str = "worker-a", **kwargs):
    return process_outbox_once(port, sender, now=now, owner=owner, **kwargs)


def _reconcile(port: InMemoryOutbox, sender: FakeSender, *, now: datetime = NOW, owner: str = "worker-a", **kwargs):
    return reconcile_outbox_once(port, sender, now=now, owner=owner, **kwargs)


def test_backoff_delay_is_exponential_capped_and_jitter_injectable() -> None:
    def identity(delay: float) -> float:
        return delay

    assert backoff_delay(1, jitter=identity) == timedelta(seconds=1)
    assert backoff_delay(2, jitter=identity) == timedelta(seconds=2)
    assert backoff_delay(12, jitter=identity) == timedelta(seconds=30 * 60)
    halved = backoff_delay(3, jitter=lambda delay: delay / 2)
    assert halved == timedelta(seconds=2)
    with pytest.raises(ValueError, match="从 1 起算"):
        backoff_delay(0, jitter=identity)


def test_classify_send_error_maps_known_types_and_rejects_unknown() -> None:
    assert classify_send_error(NotifyThrottledError(7)) == "retry"
    assert classify_send_error(NotifyUnavailableError("503")) == "retry"
    assert classify_send_error(NotifyDedupConflictError()) == "fail"
    assert classify_send_error(NotifyRejectedError("401")) == "fail"
    with pytest.raises(TypeError, match="未分类"):
        classify_send_error(RuntimeError("boom"))


def test_map_provider_recipient_status() -> None:
    assert map_provider_recipient_status("pending") == STATUS_ACCEPTED
    assert map_provider_recipient_status("throttled") == STATUS_ACCEPTED
    assert map_provider_recipient_status("sent") == STATUS_SENT
    assert map_provider_recipient_status("delivered") == STATUS_DELIVERED
    assert map_provider_recipient_status("failed") == STATUS_FAILED
    with pytest.raises(ValueError, match="未知的上游收件人状态"):
        map_provider_recipient_status("read")


def test_aggregate_local_status_rules() -> None:
    assert aggregate_local_status([STATUS_DELIVERED, STATUS_DELIVERED]) == AggregatedLocalStatus(STATUS_DELIVERED)
    assert aggregate_local_status([STATUS_FAILED, STATUS_FAILED]) == AggregatedLocalStatus(STATUS_FAILED)
    mixed = aggregate_local_status([STATUS_FAILED, STATUS_SENT])
    assert mixed == AggregatedLocalStatus(STATUS_SENT, partial=True)
    delivered_and_failed = aggregate_local_status([STATUS_DELIVERED, STATUS_FAILED])
    assert delivered_and_failed == AggregatedLocalStatus(STATUS_SENT, partial=True)
    assert aggregate_local_status([STATUS_SENT, STATUS_DELIVERED]) == AggregatedLocalStatus(STATUS_SENT)
    still_accepted = aggregate_local_status([STATUS_ACCEPTED, STATUS_ACCEPTED])
    assert still_accepted == AggregatedLocalStatus(STATUS_ACCEPTED)
    partial_accept = aggregate_local_status([STATUS_ACCEPTED, STATUS_FAILED])
    assert partial_accept == AggregatedLocalStatus(STATUS_ACCEPTED, partial=True)
    with pytest.raises(ValueError, match="不能为空"):
        aggregate_local_status([])
    with pytest.raises(ValueError, match="未知的本地收件人状态"):
        aggregate_local_status(["queued"])


def test_notification_delivery_status_schema() -> None:
    value = NotificationDeliveryStatus(
        status="accepted",
        provider_message_id="msg-1",
        recipient_count=2,
        last_reconciled_at=NOW,
    )
    dumped = value.model_dump(by_alias=True)
    assert dumped["providerMessageId"] == "msg-1"
    assert dumped["recipientCount"] == 2
    assert dumped["lastReconciledAt"] == NOW


def test_process_202_marks_accepted_with_message_id() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=FakeSendResult(message_id="msg-202", accepted=True, status="pending"))
    summary = _process(port, sender)
    item = port.get("item-1")
    assert summary == ProcessSummary(claimed=1, accepted=1, retried=0, failed=0)
    assert item.status == STATUS_ACCEPTED
    assert item.provider_message_id == "msg-202"
    assert item.lease_owner is None


def test_process_200_accepted_false_is_idempotent_accept() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=FakeSendResult(message_id="msg-first", accepted=False, status="pending"))
    summary = _process(port, sender)
    item = port.get("item-1")
    assert summary.accepted == 1
    assert item.status == STATUS_ACCEPTED
    assert item.provider_message_id == "msg-first"


def test_process_409_fails_permanently_with_dedup_conflict() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=NotifyDedupConflictError("payload mismatch"))
    summary = _process(port, sender)
    item = port.get("item-1")
    assert summary.failed == 1
    assert item.status == STATUS_FAILED
    assert item.last_error == ERROR_DEDUP_CONFLICT


@pytest.mark.parametrize("exc", [NotifyRejectedError("401"), NotifyRejectedError("403"), NotifyRejectedError("422")])
def test_process_permanent_reject(exc: NotifyRejectedError) -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    summary = _process(port, FakeSender(send=exc))
    item = port.get("item-1")
    assert summary.failed == 1
    assert item.status == STATUS_FAILED
    assert item.last_error == ERROR_REJECTED


def test_process_429_retries_at_retry_after() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=NotifyThrottledError(90))
    summary = _process(port, sender, jitter=lambda delay: delay)
    item = port.get("item-1")
    assert summary.retried == 1
    assert item.status == STATUS_QUEUED
    assert item.last_error == ERROR_THROTTLED
    assert item.next_attempt_at == NOW + timedelta(seconds=90)
    assert item.attempt_count == 1
    later = _process(port, sender, now=NOW + timedelta(seconds=89))
    assert later.claimed == 0
    due = _process(port, sender, now=NOW + timedelta(seconds=90))
    assert due.claimed == 1


def test_process_429_without_retry_after_uses_backoff() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=NotifyThrottledError())
    _process(port, sender, jitter=lambda delay: delay)
    item = port.get("item-1")
    assert item.next_attempt_at == NOW + timedelta(seconds=1)
    assert item.last_error == ERROR_THROTTLED


@pytest.mark.parametrize("exc", [NotifyUnavailableError("network"), NotifyUnavailableError("503")])
def test_process_unavailable_retries_with_backoff(exc: NotifyUnavailableError) -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    summary = _process(port, FakeSender(send=exc), jitter=lambda delay: delay)
    item = port.get("item-1")
    assert summary.retried == 1
    assert item.status == STATUS_QUEUED
    assert item.last_error == ERROR_UNAVAILABLE
    assert item.next_attempt_at == NOW + timedelta(seconds=1)


def test_process_exhausts_after_max_attempts_default_12() -> None:
    assert DEFAULT_MAX_ATTEMPTS == 12
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=NotifyUnavailableError("down"))
    now = NOW
    for step in range(DEFAULT_MAX_ATTEMPTS - 1):
        summary = _process(port, sender, now=now, jitter=lambda delay: delay, lease_seconds=1)
        assert summary.retried == 1
        assert port.get("item-1").status == STATUS_QUEUED
        now = port.get("item-1").next_attempt_at
        assert now is not None
        assert port.get("item-1").attempt_count == step + 1
    summary = _process(port, sender, now=now, jitter=lambda delay: delay, lease_seconds=1)
    item = port.get("item-1")
    assert summary.failed == 1
    assert item.status == STATUS_FAILED
    assert item.last_error == ERROR_EXHAUSTED
    assert item.attempt_count == DEFAULT_MAX_ATTEMPTS


def test_process_max_attempts_configurable() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=NotifyUnavailableError("down"))
    first = _process(port, sender, max_attempts=2, jitter=lambda delay: delay)
    assert first.retried == 1
    now = port.get("item-1").next_attempt_at
    second = _process(port, sender, now=now, max_attempts=2, jitter=lambda delay: delay)
    assert second.failed == 1
    assert port.get("item-1").last_error == ERROR_EXHAUSTED


def test_process_unknown_error_fails_fast_without_marking_failed() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    with pytest.raises(TypeError, match="未分类"):
        _process(port, FakeSender(send=RuntimeError("bug")))
    item = port.get("item-1")
    assert item.status == STATUS_QUEUED
    assert item.last_error is None


def test_process_missing_message_id_releases_and_raises() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(send=FakeSendResult(message_id="", accepted=True))
    with pytest.raises(ValueError, match="必须返回 message_id"):
        _process(port, sender)
    item = port.get("item-1")
    assert item.status == STATUS_QUEUED
    assert item.lease_owner is None


def test_claimed_items_are_not_reclaimed_until_lease_expiry() -> None:
    port = InMemoryOutbox()
    port.put(_queued("item-1"))
    port.put(_queued("item-2", dedup_key="dedup-2"))
    first = port.claim_due(1, NOW, 30, "worker-a")
    assert [item.id for item in first] == ["item-1"]
    assert first[0].lease_owner == "worker-a"
    assert first[0].lease_token
    second = port.claim_due(10, NOW, 30, "worker-b")
    assert [item.id for item in second] == ["item-2"]
    assert port.claim_due(10, NOW, 30, "worker-a") == []
    still_held = port.claim_due(10, NOW + timedelta(seconds=29), 30, "worker-c")
    assert still_held == []
    expired = port.claim_due(10, NOW + timedelta(seconds=30), 30, "worker-c")
    assert {item.id for item in expired} == {"item-1", "item-2"}
    assert {item.lease_owner for item in expired} == {"worker-c"}


def test_release_allows_reclaim_before_lease_expiry() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    claimed = port.claim_due(1, NOW, 30, "worker-a")
    assert claimed[0].lease_owner == "worker-a"
    token = claimed[0].lease_token
    assert token is not None
    port.release("item-1", token)
    reclaimed = port.claim_due(1, NOW, 30, "worker-b")
    assert reclaimed[0].lease_owner == "worker-b"


def test_claim_due_skips_future_retry_and_non_queued() -> None:
    port = InMemoryOutbox()
    port.put(replace(_queued("future"), next_attempt_at=NOW + timedelta(minutes=5)))
    port.put(replace(_queued("failed"), status=STATUS_FAILED))
    port.put(replace(_queued("superseded"), status=STATUS_SUPERSEDED))
    assert port.claim_due(10, NOW, 30, "worker-a") == []


def _accepted(item_id: str = "item-1", message_id: str = "msg-1") -> OutboxItem:
    return replace(_queued(item_id), status=STATUS_ACCEPTED, provider_message_id=message_id)


def test_reconcile_maps_sent_delivered_failed_and_advances_timestamp() -> None:
    port = InMemoryOutbox()
    port.put(_accepted("sent-item", "msg-sent"))
    port.put(_accepted("delivered-item", "msg-delivered"))
    port.put(_accepted("failed-item", "msg-failed"))
    sender = FakeSender(
        messages={
            "msg-sent": FakeMessageStatus("completed", (FakeRecipient("sent"),)),
            "msg-delivered": FakeMessageStatus("completed", (FakeRecipient("delivered"),)),
            "msg-failed": FakeMessageStatus("failed", (FakeRecipient("failed"),)),
        }
    )
    summary = _reconcile(port, sender, now=NOW)
    assert summary.claimed == 3
    assert summary.applied == 3
    assert port.get("sent-item").status == STATUS_SENT
    assert port.get("delivered-item").status == STATUS_DELIVERED
    assert port.get("failed-item").status == STATUS_FAILED
    assert port.get("sent-item").last_reconciled_at == NOW
    assert port.get("delivered-item").last_reconciled_at == NOW
    assert port.get("failed-item").last_reconciled_at == NOW


def test_reconcile_mixed_failed_and_sent_stays_sent() -> None:
    port = InMemoryOutbox()
    port.put(_accepted())
    sender = FakeSender(
        messages={
            "msg-1": FakeMessageStatus(
                "partially_failed",
                (FakeRecipient("sent"), FakeRecipient("failed")),
            )
        }
    )
    _reconcile(port, sender, now=NOW)
    assert port.get("item-1").status == STATUS_SENT
    assert port.get("item-1").last_reconciled_at == NOW


def test_reconcile_always_advances_last_reconciled_at_when_still_sent() -> None:
    port = InMemoryOutbox()
    port.put(_accepted())
    sender = FakeSender(messages={"msg-1": FakeMessageStatus("sending", (FakeRecipient("sent"),))})
    first = NOW
    _reconcile(port, sender, now=first)
    later = first + timedelta(minutes=1)
    _reconcile(port, sender, now=later)
    item = port.get("item-1")
    assert item.status == STATUS_SENT
    assert item.last_reconciled_at == later


def test_sent_item_older_than_24h_stays_sent_and_leaves_reconcile_queue() -> None:
    port = InMemoryOutbox()
    sent_at = NOW - RECONCILE_HORIZON
    port.put(
        replace(
            _queued(),
            status=STATUS_SENT,
            provider_message_id="msg-1",
            last_reconciled_at=sent_at,
        ),
        sent_at=sent_at,
    )
    sender = FakeSender(messages={"msg-1": FakeMessageStatus("completed", (FakeRecipient("delivered"),))})
    summary = _reconcile(port, sender, now=NOW)
    assert summary.claimed == 0
    assert sender.get_calls == []
    item = port.get("item-1")
    assert item.status == STATUS_SENT
    assert item.last_reconciled_at == sent_at


def test_sent_item_within_24h_can_become_delivered() -> None:
    port = InMemoryOutbox()
    sent_at = NOW - timedelta(hours=23)
    port.put(
        replace(
            _queued(),
            status=STATUS_SENT,
            provider_message_id="msg-1",
            last_reconciled_at=sent_at,
        ),
        sent_at=sent_at,
    )
    sender = FakeSender(messages={"msg-1": FakeMessageStatus("completed", (FakeRecipient("delivered"),))})
    _reconcile(port, sender, now=NOW)
    assert port.get("item-1").status == STATUS_DELIVERED


def test_reconcile_does_not_infer_delivered_from_age_alone() -> None:
    port = InMemoryOutbox()
    sent_at = NOW - timedelta(hours=10)
    port.put(
        replace(
            _queued(),
            status=STATUS_SENT,
            provider_message_id="msg-1",
            last_reconciled_at=sent_at,
        ),
        sent_at=sent_at,
    )
    sender = FakeSender(messages={"msg-1": FakeMessageStatus("completed", (FakeRecipient("sent"),))})
    _reconcile(port, sender, now=NOW)
    assert port.get("item-1").status == STATUS_SENT


def test_sent_reconcile_expired_helper() -> None:
    sent_at = NOW - RECONCILE_HORIZON
    assert sent_reconcile_expired(sent_at, NOW) is True
    assert sent_reconcile_expired(NOW - RECONCILE_HORIZON + timedelta(seconds=1), NOW) is False
    with pytest.raises(ValueError, match="必须带时区"):
        sent_reconcile_expired(datetime(2026, 9, 4), NOW)


def test_claim_reconcile_prefers_longest_unreconciled() -> None:
    port = InMemoryOutbox()
    older = replace(_accepted("old", "msg-old"), last_reconciled_at=NOW - timedelta(minutes=10))
    newer = replace(_accepted("new", "msg-new"), last_reconciled_at=NOW - timedelta(minutes=1))
    never = _accepted("never", "msg-never")
    port.put(newer)
    port.put(older)
    port.put(never)
    order = [item.id for item in port.claim_reconcile_due(2, NOW, 30, "worker-a")]
    assert order == ["never", "old"]


def test_process_then_reconcile_end_to_end() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    sender = FakeSender(
        send=FakeSendResult(message_id="msg-1", accepted=True),
        messages={"msg-1": FakeMessageStatus("pending", (FakeRecipient("pending"),))},
    )
    process_outbox_once(port, sender, now=NOW, owner="worker-a")
    assert port.get("item-1").status == STATUS_ACCEPTED
    _reconcile(port, sender, now=NOW + timedelta(seconds=5))
    assert port.get("item-1").status == STATUS_ACCEPTED
    sender.messages["msg-1"] = FakeMessageStatus("completed", (FakeRecipient("delivered"),))
    _reconcile(port, sender, now=NOW + timedelta(minutes=2))
    assert port.get("item-1").status == STATUS_DELIVERED


def test_process_dedup_key_mismatch_fails_without_send() -> None:
    port = InMemoryOutbox()
    port.put(replace(_queued(), payload=FakeNotifyRequest(dedup_key="other-key")))
    sender = FakeSender(send=FakeSendResult(message_id="msg-1", accepted=True))
    summary = _process(port, sender)
    assert summary == ProcessSummary(claimed=1, accepted=0, retried=0, failed=1)
    assert sender.send_calls == []
    item = port.get("item-1")
    assert item.status == STATUS_FAILED
    assert item.last_error == ERROR_DEDUP_KEY_MISMATCH


def test_stale_lease_retry_does_not_clobber_newer_owner() -> None:
    port = InMemoryOutbox()
    port.put(_queued())
    worker_a = port.claim_due(1, NOW, 30, "worker-a")
    a_token = worker_a[0].lease_token
    assert a_token
    later = NOW + timedelta(seconds=30)
    worker_b = port.claim_due(1, later, 30, "worker-b")
    b_token = worker_b[0].lease_token
    assert b_token
    assert b_token != a_token
    assert port.mark_accepted("item-1", "msg-b", later, b_token) is True
    assert port.mark_retry("item-1", later + timedelta(minutes=1), ERROR_UNAVAILABLE, a_token) is False
    item = port.get("item-1")
    assert item.status == STATUS_ACCEPTED
    assert item.provider_message_id == "msg-b"
    assert item.attempt_count == 0
    assert item.last_error is None


def test_apply_provider_status_keeps_delivered_against_stale_sent() -> None:
    port = InMemoryOutbox()
    port.put(_accepted())
    claimed = port.claim_reconcile_due(1, NOW, 30, "worker-a")
    token = claimed[0].lease_token
    assert token is not None
    delivered = FakeMessageStatus("completed", (FakeRecipient("delivered"),))
    stale_sent = FakeMessageStatus("completed", (FakeRecipient("sent"),))
    assert port.apply_provider_status("item-1", delivered, NOW, token) is True
    assert port.get("item-1").status == STATUS_DELIVERED
    later = NOW + timedelta(seconds=1)
    assert port.apply_provider_status("item-1", stale_sent, later, token) is True
    item = port.get("item-1")
    assert item.status == STATUS_DELIVERED
    assert item.last_reconciled_at == later


def test_reconcile_continues_after_first_item_error() -> None:
    port = InMemoryOutbox()
    port.put(_accepted("item-1", "msg-1"))
    port.put(_accepted("item-2", "msg-2"))

    class PartialSender(FakeSender):
        def get_message(self, message_id: str) -> FakeMessageStatus:
            self.get_calls.append(message_id)
            if message_id == "msg-1":
                raise RuntimeError("upstream boom")
            return FakeMessageStatus("completed", (FakeRecipient("delivered"),))

    summary = _reconcile(port, PartialSender())
    assert summary.claimed == 2
    assert summary.applied == 1
    first = port.get("item-1")
    assert first.status == STATUS_ACCEPTED
    assert first.last_reconciled_at == NOW
    assert first.last_error == "upstream boom"
    assert first.lease_owner is None
    assert port.get("item-2").status == STATUS_DELIVERED


def test_reconcile_provider_404_marks_message_missing() -> None:
    port = InMemoryOutbox()
    port.put(_accepted("gone", "msg-gone"))
    port.put(_accepted("ok", "msg-ok"))

    class MissingThenOk(FakeSender):
        def get_message(self, message_id: str) -> FakeMessageStatus:
            self.get_calls.append(message_id)
            if message_id == "msg-gone":
                raise NotifyRejectedError("not found", status=404)
            return FakeMessageStatus("completed", (FakeRecipient("delivered"),))

    summary = _reconcile(port, MissingThenOk())
    assert summary.claimed == 2
    assert summary.applied == 1
    missing = port.get("gone")
    assert missing.status == STATUS_FAILED
    assert missing.last_error == ERROR_PROVIDER_MESSAGE_MISSING
    assert port.get("ok").status == STATUS_DELIVERED


def test_notify_client_satisfies_sender() -> None:
    import httpx

    from enterprise_platform.easyauth import EasyAuthCredential, NotifyClient, NotifyRequest

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/apps/easytrade/notify/messages"
        return httpx.Response(
            202,
            json={
                "message_id": "msg-client",
                "accepted": True,
                "status": "pending",
                "recipient_total": 1,
                "recipient_rejected": 0,
            },
        )

    payload = NotifyRequest(
        recipients=("dt:v1:demo",),
        template="text",
        content="hello",
        dedup_key="dedup-1",
    )
    port = InMemoryOutbox()
    port.put(
        OutboxItem(
            id="item-1",
            dedup_key="dedup-1",
            payload=payload,
            status=STATUS_QUEUED,
            attempt_count=0,
            next_attempt_at=None,
            provider_message_id=None,
            last_error=None,
            lease_owner=None,
            lease_expires_at=None,
            last_reconciled_at=None,
            lease_token=None,
        )
    )
    client = NotifyClient(
        EasyAuthCredential(
            base_url="https://easyauth.example.test",
            app_key="easytrade",
            auth_mode="static_app_token",
            credential="eat_notify_test",
        ),
        transport=httpx.MockTransport(handler),
    )
    summary = process_outbox_once(port, client, now=NOW, owner="worker-a")
    assert summary == ProcessSummary(claimed=1, accepted=1, retried=0, failed=0)
    item = port.get("item-1")
    assert item.status == STATUS_ACCEPTED
    assert item.provider_message_id == "msg-client"
