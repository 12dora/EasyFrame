"""EasyAuth 通知凭据探活:假 transport,不发钉钉、不打真实网络。"""

from __future__ import annotations

import httpx

from enterprise_platform.easyauth import PROBE_MESSAGE_ID, probe_notify_credential

BASE_URL = "https://easyauth.example.test"
APP_KEY = "enterprise-blank"
TOKEN = "eat_notify_probe"


def _error_payload(code: str) -> dict[str, str | dict[str, str]]:
    return {"error": {"code": code, "message": "denied"}}


def test_probe_404_is_ok() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.method == "GET"
        assert PROBE_MESSAGE_ID in str(request.url)
        return httpx.Response(404, json=_error_payload("NOT_FOUND"))

    result = probe_notify_credential(BASE_URL, APP_KEY, TOKEN, transport=httpx.MockTransport(handler))
    assert result.ok is True
    assert calls["n"] == 1


def test_probe_401_is_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_payload("AUTHENTICATION_FAILED"))

    result = probe_notify_credential(BASE_URL, APP_KEY, TOKEN, transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert result.error_kind == "auth"


def test_probe_403_is_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_error_payload("FORBIDDEN"))

    result = probe_notify_credential(BASE_URL, APP_KEY, TOKEN, transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert result.error_kind == "auth"


def test_probe_connect_error_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = probe_notify_credential(BASE_URL, APP_KEY, TOKEN, transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert result.error_kind == "unreachable"


def test_probe_not_configured_skips_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("not configured must not call EasyAuth")

    result = probe_notify_credential("", "", "", transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert result.error_kind == "not_configured"
