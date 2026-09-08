"""EasyAuth webhook 验签:无网络、无数据库。"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_platform.easyauth.webhook import (
    REASON_EVENT_TYPE_MISMATCH,
    REASON_INVALID_PAYLOAD,
    REASON_INVALID_TIMESTAMP,
    REASON_MISSING_HEADERS,
    REASON_MISSING_SECRET,
    REASON_SIGNATURE_MISMATCH,
    REASON_TIMESTAMP_SKEW,
    WebhookVerificationError,
    verify_webhook,
)

SECRET = "whsec_test"
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def _sign(body: bytes, *, event: str = "webhook.test", timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(NOW.timestamp()) if timestamp is None else timestamp)
    signature = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "X-EasyAuth-Event": event,
        "X-EasyAuth-Delivery": "delivery-1",
        "X-EasyAuth-Timestamp": ts,
        "X-EasyAuth-Signature": signature,
    }


def _body(event_type: str = "webhook.test", **extra: object) -> bytes:
    return json.dumps({"event_type": event_type, **extra}, sort_keys=True).encode()


def test_verify_webhook_roundtrip() -> None:
    body = _body("grant.changed", user_id="u1", snapshot_version="1.2")
    event = verify_webhook(_sign(body, event="grant.changed"), body, SECRET, NOW)
    assert event.event_type == "grant.changed"
    assert event.delivery_id == "delivery-1"
    assert event.payload["user_id"] == "u1"


def test_verify_webhook_accepts_lowercase_headers() -> None:
    body = _body()
    headers = {key.lower(): value for key, value in _sign(body).items()}
    event = verify_webhook(headers, body, SECRET, NOW)
    assert event.event_type == "webhook.test"


def test_verify_webhook_rejects_empty_secret() -> None:
    body = _body()
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body), body, "", NOW)
    assert captured.value.reason == REASON_MISSING_SECRET


def test_verify_webhook_rejects_missing_headers() -> None:
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook({}, _body(), SECRET, NOW)
    assert captured.value.reason == REASON_MISSING_HEADERS


def test_verify_webhook_rejects_invalid_timestamp() -> None:
    body = _body()
    headers = _sign(body)
    headers["X-EasyAuth-Timestamp"] = "not-a-unix-ts"
    headers["X-EasyAuth-Signature"] = hmac.new(
        SECRET.encode(),
        b"not-a-unix-ts." + body,
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(headers, body, SECRET, NOW)
    assert captured.value.reason == REASON_INVALID_TIMESTAMP


def test_verify_webhook_rejects_timestamp_skew() -> None:
    body = _body()
    stale = int((NOW - timedelta(seconds=301)).timestamp())
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body, timestamp=stale), body, SECRET, NOW)
    assert captured.value.reason == REASON_TIMESTAMP_SKEW


def test_verify_webhook_accepts_timestamp_at_window_edge() -> None:
    body = _body()
    edge = int((NOW - timedelta(seconds=300)).timestamp())
    event = verify_webhook(_sign(body, timestamp=edge), body, SECRET, NOW)
    assert event.event_type == "webhook.test"


def test_verify_webhook_rejects_bad_signature() -> None:
    body = _body()
    headers = _sign(body)
    headers["X-EasyAuth-Signature"] = "0" * 64
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(headers, body, SECRET, NOW)
    assert captured.value.reason == REASON_SIGNATURE_MISMATCH


def test_verify_webhook_rejects_tampered_body() -> None:
    body = _body()
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body), _body(note="tampered"), SECRET, NOW)
    assert captured.value.reason == REASON_SIGNATURE_MISMATCH


def test_verify_webhook_rejects_event_header_mismatch() -> None:
    body = _body("grant.changed")
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body, event="catalog.changed"), body, SECRET, NOW)
    assert captured.value.reason == REASON_EVENT_TYPE_MISMATCH


def test_verify_webhook_rejects_non_object_payload() -> None:
    body = b"[]"
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body), body, SECRET, NOW)
    assert captured.value.reason == REASON_INVALID_PAYLOAD


def test_verify_webhook_rejects_invalid_json() -> None:
    body = b"{not-json"
    with pytest.raises(WebhookVerificationError) as captured:
        verify_webhook(_sign(body), body, SECRET, NOW)
    assert captured.value.reason == REASON_INVALID_PAYLOAD
