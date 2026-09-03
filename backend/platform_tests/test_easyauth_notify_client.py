"""EasyAuth NotifyClient:假 transport,无网络、无数据库。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from enterprise_platform.easyauth import (
    EasyAuthCredential,
    EasyAuthProtocolError,
    NotifyClient,
    NotifyDedupConflictError,
    NotifyProtocolError,
    NotifyRejectedError,
    NotifyRequest,
    NotifyThrottledError,
    NotifyUnavailableError,
)

BASE_URL = "https://easyauth.example.test"
APP_KEY = "easytrade"
TOKEN = "eat_notify_test"
MESSAGE_ID = "0d9f5c1e-7a42-4b8e-9c3d-2f1a6b8e4d70"
USER_REF = "dt:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:dXNlcjAxMjM"


def _credential() -> EasyAuthCredential:
    return EasyAuthCredential(
        base_url=BASE_URL,
        app_key=APP_KEY,
        auth_mode="static_app_token",
        credential=TOKEN,
    )


def _client(handler) -> NotifyClient:
    return NotifyClient(_credential(), transport=httpx.MockTransport(handler))


def _request() -> NotifyRequest:
    return NotifyRequest(
        recipients=(USER_REF,),
        template="action_card",
        content="### 任务已逾期",
        title="任务逾期升级",
        deeplink_url="https://app.example/tasks/123",
        deeplink_title="查看任务",
        dedup_key="overdue-escalate:123:2026-07-16",
        biz_tag="overdue_escalation",
    )


def _accepted_payload(*, accepted: bool = True) -> dict[str, Any]:
    return {
        "message_id": MESSAGE_ID,
        "accepted": accepted,
        "status": "pending",
        "recipient_total": 1,
        "recipient_rejected": 0,
    }


def _error_payload(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": "denied", "details": {}}}


def test_send_maps_202_as_new_acceptance() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json=_accepted_payload(accepted=True))

    result = _client(handler).send(_request())
    assert captured["authorization"] == f"Bearer {TOKEN}"
    assert captured["path"] == f"/api/v1/apps/{APP_KEY}/notify/messages"
    assert captured["body"]["recipients"] == [USER_REF]
    assert captured["body"]["dedup_key"] == "overdue-escalate:123:2026-07-16"
    assert result.message_id == MESSAGE_ID
    assert result.accepted is True
    assert result.status == "pending"
    assert result.recipient_total == 1
    assert result.recipient_rejected == 0


def test_send_maps_200_duplicate_as_idempotent_replay() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_accepted_payload(accepted=False))

    result = _client(handler).send(_request())
    assert result.accepted is False
    assert result.message_id == MESSAGE_ID


def test_send_maps_409_as_dedup_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=_error_payload("CONFLICT"))

    with pytest.raises(NotifyDedupConflictError):
        _client(handler).send(_request())


def test_send_maps_422_as_permanent_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=_error_payload("VALIDATION_ERROR"))

    with pytest.raises(NotifyRejectedError) as captured:
        _client(handler).send(_request())
    assert captured.value.status == 422
    assert captured.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize(("status", "code"), [(401, "AUTHENTICATION_FAILED"), (403, "PERMISSION_DENIED")])
def test_send_maps_401_403_as_permanent_rejection(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=_error_payload(code))

    with pytest.raises(NotifyRejectedError) as captured:
        _client(handler).send(_request())
    assert captured.value.status == status
    assert captured.value.code == code


def test_send_maps_429_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "27"}, json=_error_payload("THROTTLED"))

    with pytest.raises(NotifyThrottledError) as captured:
        _client(handler).send(_request())
    assert captured.value.retry_after == 27


def test_send_maps_503_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_error_payload("DEPENDENCY_UNAVAILABLE"))

    with pytest.raises(NotifyUnavailableError, match="503"):
        _client(handler).send(_request())


def test_get_message_parses_recipient_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/apps/{APP_KEY}/notify/messages/{MESSAGE_ID}"
        return httpx.Response(
            200,
            json={
                "message_id": MESSAGE_ID,
                "status": "partially_failed",
                "completed_at": "2026-07-16T10:03:12+08:00",
                "recipients": [
                    {
                        "raw_ref": USER_REF,
                        "user_id": "f7c31a09e5b24f8d9a1c",
                        "dingtalk_user_id": "user0123",
                        "status": "delivered",
                        "error_code": "",
                        "error": "",
                        "sent_at": "2026-07-16T10:00:04+08:00",
                        "delivered_at": "2026-07-16T10:01:00+08:00",
                    },
                    {
                        "raw_ref": "dt:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:Zm9ybWVydXNlcjAx",
                        "user_id": None,
                        "dingtalk_user_id": "formeruser01",
                        "status": "failed",
                        "error_code": "USER_INACTIVE",
                        "error": "目录状态为 departed, 拒绝投递。",
                        "sent_at": None,
                        "delivered_at": None,
                    },
                ],
            },
        )

    result = _client(handler).get_message(MESSAGE_ID)
    assert result.status == "partially_failed"
    assert result.completed_at == "2026-07-16T10:03:12+08:00"
    assert result.recipients[0].status == "delivered"
    assert result.recipients[1].user_id is None
    assert result.recipients[1].error_code == "USER_INACTIVE"


def test_get_message_keeps_other_recipient_when_identity_is_null() -> None:
    failed_ref = "dt:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:dW5yZXNvbHZlZDA"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message_id": MESSAGE_ID,
                "status": "partially_failed",
                "completed_at": "2026-07-16T10:03:12+08:00",
                "recipients": [
                    {
                        "raw_ref": USER_REF,
                        "user_id": "f7c31a09e5b24f8d9a1c",
                        "dingtalk_user_id": "user0123",
                        "status": "delivered",
                        "error_code": "",
                        "error": "",
                        "sent_at": "2026-07-16T10:00:04+08:00",
                        "delivered_at": "2026-07-16T10:01:00+08:00",
                    },
                    {
                        "raw_ref": failed_ref,
                        "user_id": None,
                        "dingtalk_user_id": None,
                        "status": "failed",
                        "error_code": "UNRESOLVED_REF",
                        "error": "未能解析收件人。",
                        "sent_at": None,
                        "delivered_at": None,
                    },
                ],
            },
        )

    result = _client(handler).get_message(MESSAGE_ID)
    assert result.recipients[0].status == "delivered"
    assert result.recipients[0].dingtalk_user_id == "user0123"
    assert result.recipients[1].raw_ref == failed_ref
    assert result.recipients[1].user_id is None
    assert result.recipients[1].dingtalk_user_id is None
    assert result.recipients[1].status == "failed"


def test_send_truncated_202_is_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b'{"message_id":', headers={"Content-Type": "application/json"})

    with pytest.raises(NotifyProtocolError) as captured:
        _client(handler).send(_request())
    assert isinstance(captured.value, NotifyUnavailableError)
    assert isinstance(captured.value, EasyAuthProtocolError)
    assert not isinstance(captured.value, NotifyRejectedError)


def test_get_message_malformed_status_body_is_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "recipients": "nope"})

    with pytest.raises(NotifyProtocolError) as captured:
        _client(handler).get_message(MESSAGE_ID)
    assert isinstance(captured.value, NotifyUnavailableError)
    assert isinstance(captured.value, EasyAuthProtocolError)
    assert not isinstance(captured.value, NotifyRejectedError)


def test_get_message_maps_503_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_error_payload("DEPENDENCY_UNAVAILABLE"))

    with pytest.raises(NotifyUnavailableError):
        _client(handler).get_message(MESSAGE_ID)
