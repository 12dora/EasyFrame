"""EasyAuth → 下游应用 webhook 验签。与 EasyAuth 服务端签名方案严格对偶。"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

SIGNATURE_HEADER = "X-EasyAuth-Signature"
TIMESTAMP_HEADER = "X-EasyAuth-Timestamp"
DELIVERY_HEADER = "X-EasyAuth-Delivery"
EVENT_HEADER = "X-EasyAuth-Event"
TIMESTAMP_WINDOW_SECONDS = 300

GRANT_CHANGED_EVENT = "grant.changed"
CATALOG_CHANGED_EVENT = "catalog.changed"
WEBHOOK_TEST_EVENT = "webhook.test"

REASON_MISSING_SECRET: Final = "MISSING_SECRET"
REASON_MISSING_HEADERS: Final = "MISSING_HEADERS"
REASON_INVALID_TIMESTAMP: Final = "INVALID_TIMESTAMP"
REASON_TIMESTAMP_SKEW: Final = "TIMESTAMP_SKEW"
REASON_SIGNATURE_MISMATCH: Final = "SIGNATURE_MISMATCH"
REASON_INVALID_PAYLOAD: Final = "INVALID_PAYLOAD"
REASON_EVENT_TYPE_MISMATCH: Final = "EVENT_TYPE_MISMATCH"


class WebhookVerificationError(RuntimeError):
    """验签失败。``reason`` 是结构化字段,调用方按它分支,不得解析 ``str(error)``。"""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class WebhookEvent:
    event_type: str
    delivery_id: str
    timestamp: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _RequiredHeaders:
    event_type: str
    delivery_id: str
    timestamp: str
    signature: str


def verify_webhook(
    headers: Mapping[str, str],
    raw_body: bytes,
    secret: str,
    now: datetime,
) -> WebhookEvent:
    """校验时间窗、HMAC 与 event 头/体一致;失败抛 WebhookVerificationError。"""

    if not secret:
        raise WebhookVerificationError("webhook secret 未配置。", reason=REASON_MISSING_SECRET)
    required = _required_headers(headers)
    timestamp = _validate_timestamp(required.timestamp, now=now)
    _validate_signature(secret, timestamp_raw=required.timestamp, signature=required.signature, raw_body=raw_body)
    payload = _parse_payload(raw_body)
    body_event = payload.get("event_type")
    if not isinstance(body_event, str) or body_event != required.event_type:
        raise WebhookVerificationError("webhook 事件头与载荷 event_type 不一致。", reason=REASON_EVENT_TYPE_MISMATCH)
    return WebhookEvent(
        event_type=required.event_type,
        delivery_id=required.delivery_id,
        timestamp=timestamp,
        payload=payload,
    )


def _required_headers(headers: Mapping[str, str]) -> _RequiredHeaders:
    normalized = {key.lower(): value for key, value in headers.items()}
    event_type = normalized.get(EVENT_HEADER.lower(), "")
    delivery_id = normalized.get(DELIVERY_HEADER.lower(), "")
    timestamp_raw = normalized.get(TIMESTAMP_HEADER.lower(), "")
    signature = normalized.get(SIGNATURE_HEADER.lower(), "")
    if not event_type or not delivery_id or not timestamp_raw or not signature:
        raise WebhookVerificationError("webhook 请求头不完整。", reason=REASON_MISSING_HEADERS)
    return _RequiredHeaders(event_type, delivery_id, timestamp_raw, signature)


def _validate_timestamp(timestamp_raw: str, *, now: datetime) -> int:
    if not timestamp_raw.isdecimal():
        raise WebhookVerificationError("webhook 时间戳无效。", reason=REASON_INVALID_TIMESTAMP)
    timestamp = int(timestamp_raw)
    current = int(now.timestamp())
    if abs(current - timestamp) > TIMESTAMP_WINDOW_SECONDS:
        raise WebhookVerificationError("webhook 时间戳超出允许窗口。", reason=REASON_TIMESTAMP_SKEW)
    return timestamp


def _validate_signature(secret: str, *, timestamp_raw: str, signature: str, raw_body: bytes) -> None:
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp_raw.encode("utf-8") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("webhook 签名不匹配。", reason=REASON_SIGNATURE_MISMATCH)


def _parse_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebhookVerificationError("webhook 载荷不是有效 JSON。", reason=REASON_INVALID_PAYLOAD) from exc
    if not isinstance(parsed, dict):
        raise WebhookVerificationError("webhook 载荷必须是 JSON 对象。", reason=REASON_INVALID_PAYLOAD)
    return parsed
