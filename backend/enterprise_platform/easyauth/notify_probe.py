"""EasyAuth 通知凭据探活:只查询不存在的消息,不发送钉钉。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from enterprise_platform.easyauth.credentials import (
    EasyAuthCredential,
    EasyAuthCredentialError,
    EasyAuthTransportError,
)
from enterprise_platform.easyauth.errors import NotifyRejectedError, NotifyUnavailableError
from enterprise_platform.easyauth.notify import NotifyClient
from enterprise_platform.schemas import ConnectionTestResult

PROBE_MESSAGE_ID = "easyframe-health-probe"
_NOT_CONFIGURED = "通知服务未配置"


def probe_notify_credential(
    base_url: str,
    app_key: str,
    credential: str,
    *,
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> ConnectionTestResult:
    if not _probe_configured(base_url, app_key, credential):
        return ConnectionTestResult(ok=False, error_kind="not_configured", error_detail=_NOT_CONFIGURED)
    started = datetime.now(UTC)
    try:
        _get_probe_message(base_url, app_key, credential, timeout=timeout, transport=transport)
    except Exception as exc:
        return _map_probe_error(exc, _latency_ms(started))
    return ConnectionTestResult(ok=True, latency_ms=_latency_ms(started))


def _probe_configured(base_url: str, app_key: str, credential: str) -> bool:
    return bool(base_url.strip() and app_key.strip() and credential.strip())


def _get_probe_message(
    base_url: str,
    app_key: str,
    credential: str,
    *,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> None:
    NotifyClient(
        EasyAuthCredential(
            base_url=base_url,
            app_key=app_key,
            auth_mode="static_app_token",
            credential=credential,
        ),
        timeout=timeout,
        transport=transport,
    ).get_message(PROBE_MESSAGE_ID)


def _map_probe_error(exc: BaseException, latency: int) -> ConnectionTestResult:
    if isinstance(exc, NotifyRejectedError):
        return _rejected_probe_result(exc, latency)
    if isinstance(exc, EasyAuthCredentialError):
        return ConnectionTestResult(
            ok=False, latency_ms=latency, error_kind="not_configured", error_detail=str(exc)[:300]
        )
    if _is_unreachable(exc):
        return ConnectionTestResult(ok=False, latency_ms=latency, error_kind="unreachable", error_detail=str(exc)[:300])
    return ConnectionTestResult(ok=False, latency_ms=latency, error_kind="unexpected", error_detail=str(exc)[:300])


def _rejected_probe_result(exc: NotifyRejectedError, latency: int) -> ConnectionTestResult:
    if exc.status == 404:
        return ConnectionTestResult(ok=True, latency_ms=latency)
    if exc.status in {401, 403}:
        return ConnectionTestResult(ok=False, latency_ms=latency, error_kind="auth", error_detail=str(exc)[:300])
    return ConnectionTestResult(ok=False, latency_ms=latency, error_kind="unexpected", error_detail=str(exc)[:300])


def _is_unreachable(exc: BaseException) -> bool:
    if isinstance(exc, (EasyAuthTransportError, httpx.HTTPError, OSError)):
        return True
    return isinstance(exc, NotifyUnavailableError) and isinstance(exc.__cause__, EasyAuthTransportError)


def _latency_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)
