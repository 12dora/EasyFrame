"""EasyAuth 钉钉工作通知客户端。不记录 email/mobile。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import quote

import httpx

from enterprise_platform.easyauth.credentials import (
    EasyAuthCredential,
    EasyAuthHttp,
    EasyAuthProtocolError,
    EasyAuthTransportError,
    error_code_of,
    parse_retry_after_seconds,
    response_object,
)
from enterprise_platform.easyauth.errors import (
    NotifyDedupConflictError,
    NotifyProtocolError,
    NotifyRejectedError,
    NotifyThrottledError,
    NotifyUnavailableError,
)

_SUCCESS_STATUSES = frozenset({200, 202})
_PERMANENT_STATUSES = frozenset({401, 403, 422})


@dataclass(frozen=True)
class NotifyRequest:
    recipients: tuple[str, ...]
    template: str
    content: str
    title: str | None = None
    deeplink_url: str | None = None
    deeplink_title: str | None = None
    dedup_key: str | None = None
    biz_tag: str | None = None


@dataclass(frozen=True)
class NotifySendResult:
    message_id: str
    accepted: bool
    status: str
    recipient_total: int
    recipient_rejected: int


@dataclass(frozen=True)
class NotifyRecipientStatus:
    raw_ref: str
    user_id: str | None
    dingtalk_user_id: str | None
    status: str
    error_code: str
    error: str
    sent_at: str | None
    delivered_at: str | None


@dataclass(frozen=True)
class NotifyMessageStatus:
    status: str
    recipients: tuple[NotifyRecipientStatus, ...]
    completed_at: str | None


class NotifyClient:
    def __init__(
        self,
        credential: EasyAuthCredential,
        *,
        timeout: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = EasyAuthHttp(credential, timeout=timeout, transport=transport)

    def send(self, request: NotifyRequest) -> NotifySendResult:
        try:
            response = self._http.request("POST", "/notify/messages", json=_notify_body(request))
        except EasyAuthTransportError as exc:
            raise NotifyUnavailableError("EasyAuth notify 不可达") from exc
        return _map_send_response(response)

    def get_message(self, message_id: str) -> NotifyMessageStatus:
        if not message_id:
            raise ValueError("message_id 不能为空")
        path = f"/notify/messages/{quote(message_id, safe='')}"
        try:
            response = self._http.request("GET", path)
        except EasyAuthTransportError as exc:
            raise NotifyUnavailableError("EasyAuth notify 不可达") from exc
        return _map_get_response(response)


def _notify_body(request: NotifyRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "recipients": list(request.recipients),
        "template": request.template,
        "content": request.content,
    }
    if request.title is not None:
        body["title"] = request.title
    if request.deeplink_url is not None:
        body["deeplink_url"] = request.deeplink_url
    if request.deeplink_title is not None:
        body["deeplink_title"] = request.deeplink_title
    if request.dedup_key is not None:
        body["dedup_key"] = request.dedup_key
    if request.biz_tag is not None:
        body["biz_tag"] = request.biz_tag
    return body


def _map_send_response(response: httpx.Response) -> NotifySendResult:
    status = response.status_code
    if status in _SUCCESS_STATUSES:
        payload = _success_object(response)
        result = _parse_send_result(payload)
        expected_accepted = status == 202
        if result.accepted is not expected_accepted:
            raise NotifyProtocolError(f"EasyAuth notify HTTP {status} 与 accepted={result.accepted} 不一致")
        return result
    _raise_notify_error(response)


def _map_get_response(response: httpx.Response) -> NotifyMessageStatus:
    if response.status_code == 200:
        return _parse_message_status(_success_object(response))
    _raise_notify_error(response)


def _raise_notify_error(response: httpx.Response) -> NoReturn:
    status = response.status_code
    code = error_code_of(response)
    if status == 409:
        raise NotifyDedupConflictError(f"EasyAuth notify dedup_key 冲突 {code}".strip())
    if status in _PERMANENT_STATUSES:
        raise NotifyRejectedError(
            f"EasyAuth notify 永久拒绝 HTTP {status} {code}".strip(),
            status=status,
            code=code,
        )
    if status == 429:
        raise NotifyThrottledError(parse_retry_after_seconds(response))
    if status == 503 or status >= 500:
        raise NotifyUnavailableError(f"EasyAuth notify 返回 HTTP {status} {code}".strip())
    if status >= 400:
        raise NotifyRejectedError(
            f"EasyAuth notify 永久拒绝 HTTP {status} {code}".strip(),
            status=status,
            code=code,
        )
    raise NotifyUnavailableError(f"EasyAuth notify 返回 HTTP {status}")


def _success_object(response: httpx.Response) -> dict[str, Any]:
    try:
        return response_object(response)
    except EasyAuthProtocolError as exc:
        raise NotifyProtocolError("EasyAuth notify 响应不是 JSON 对象") from exc


def _parse_send_result(payload: dict[str, Any]) -> NotifySendResult:
    message_id = _require_str(payload.get("message_id"), "message_id")
    if not message_id:
        raise NotifyProtocolError("EasyAuth notify 响应缺少 message_id")
    return NotifySendResult(
        message_id=message_id,
        accepted=_require_bool(payload.get("accepted"), "accepted"),
        status=_require_str(payload.get("status"), "status"),
        recipient_total=_require_int(payload.get("recipient_total"), "recipient_total"),
        recipient_rejected=_require_int(payload.get("recipient_rejected"), "recipient_rejected"),
    )


def _parse_message_status(payload: dict[str, Any]) -> NotifyMessageStatus:
    raw_recipients = payload.get("recipients")
    if not isinstance(raw_recipients, list):
        raise NotifyProtocolError("EasyAuth notify 响应缺少 recipients 数组")
    return NotifyMessageStatus(
        status=_require_str(payload.get("status"), "status"),
        recipients=tuple(_parse_recipient(item) for item in raw_recipients),
        completed_at=_optional_str(payload.get("completed_at"), "completed_at"),
    )


def _parse_recipient(item: Any) -> NotifyRecipientStatus:
    if not isinstance(item, dict):
        raise NotifyProtocolError("EasyAuth notify recipients 条目必须是对象")
    raw_ref = _require_str(item.get("raw_ref"), "raw_ref")
    if not raw_ref:
        raise NotifyProtocolError("raw_ref 不能为空")
    return NotifyRecipientStatus(
        raw_ref=raw_ref,
        user_id=_optional_str(item.get("user_id"), "user_id"),
        dingtalk_user_id=_optional_str(item.get("dingtalk_user_id"), "dingtalk_user_id"),
        status=_require_str(item.get("status"), "status"),
        error_code=_require_str(item.get("error_code"), "error_code"),
        error=_require_str(item.get("error"), "error"),
        sent_at=_optional_str(item.get("sent_at"), "sent_at"),
        delivered_at=_optional_str(item.get("delivered_at"), "delivered_at"),
    )


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise NotifyProtocolError(f"{field} 必须是字符串")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, field)


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise NotifyProtocolError(f"{field} 必须是布尔值")
    return value


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotifyProtocolError(f"{field} 必须是整数")
    return value
