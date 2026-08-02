"""EasyAuth 连接探测的稳定错误分类合同。"""

from __future__ import annotations

from enum import StrEnum


class ConnectionErrorKind(StrEnum):
    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    FORBIDDEN = "forbidden"
    INVALID_RESPONSE = "invalid_response"


_NOT_CONFIGURED_MARKERS = ("is not configured", "unsupported easyauth auth mode")


def classify_connection_failure(
    message: str,
    *,
    forbidden: bool = False,
    network_error: bool = False,
) -> ConnectionErrorKind:
    """把宿主 client 错误压缩为前后端可稳定消费的五类状态。"""

    normalized = message.lower()
    if any(marker in normalized for marker in _NOT_CONFIGURED_MARKERS):
        return ConnectionErrorKind.NOT_CONFIGURED
    if forbidden:
        return ConnectionErrorKind.FORBIDDEN
    if network_error or "upstream service failed" in normalized:
        return ConnectionErrorKind.UNREACHABLE
    if "unauthorized" in normalized:
        return ConnectionErrorKind.AUTH_FAILED
    return ConnectionErrorKind.INVALID_RESPONSE
