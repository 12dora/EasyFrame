"""EasyAuth 应用凭据与受信 HTTP 会话。

Bearer 解析当前只实现 ``static_app_token``。``oauth_client_credentials`` 在既有
``authz/client.py`` 中同样没有 token provider,此处同样 fail-closed,不得把
OAuth 凭据当静态 token 使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from enterprise_platform.safe_http import UnsafeOutboundUrlError
from enterprise_platform.trusted_http import create_trusted_authority_transport

STATIC_APP_TOKEN = "static_app_token"
OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"
SUPPORTED_AUTH_MODES = frozenset({STATIC_APP_TOKEN, OAUTH_CLIENT_CREDENTIALS})


class EasyAuthCredentialError(RuntimeError):
    """凭据缺失或 auth_mode 无法解析 Bearer。"""


class EasyAuthTransportError(RuntimeError):
    """出站失败(网络、URL 守卫、超时)。不含响应体,避免把 email/mobile 打进日志。"""


class EasyAuthProtocolError(RuntimeError):
    """成功响应不是预期 JSON 对象。"""


@dataclass(frozen=True)
class EasyAuthCredential:
    base_url: str
    app_key: str
    auth_mode: Literal["static_app_token", "oauth_client_credentials"]
    credential: str


def resolve_bearer_token(credential: EasyAuthCredential) -> str:
    """把凭据解析为 Authorization Bearer 明文。失败必须抛错,禁止回退。"""

    if not credential.base_url.strip() or not credential.app_key.strip() or not credential.credential.strip():
        raise EasyAuthCredentialError("EasyAuth 未配置")
    if credential.auth_mode == STATIC_APP_TOKEN:
        return credential.credential
    if credential.auth_mode == OAUTH_CLIENT_CREDENTIALS:
        raise EasyAuthCredentialError("EasyAuth OAuth token provider 未配置")
    raise EasyAuthCredentialError(f"不支持的 EasyAuth auth_mode: {credential.auth_mode}")


def app_resource_url(credential: EasyAuthCredential, resource_path: str) -> str:
    base = credential.base_url.rstrip("/")
    app_key = quote(credential.app_key, safe="")
    path = resource_path if resource_path.startswith("/") else f"/{resource_path}"
    return f"{base}/api/v1/apps/{app_key}{path}"


def error_code_of(response: httpx.Response) -> str:
    """读取统一错误 JSON 的 ``error.code``;解析失败返回空串,不把响应体拼进消息。"""

    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def parse_retry_after_seconds(response: httpx.Response) -> int | None:
    """只接受非负整数秒;HTTP-date 不猜测。"""

    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isascii() or not text.isdecimal():
        return None
    return int(text)


def response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise EasyAuthProtocolError("EasyAuth 响应不是 JSON") from exc
    if not isinstance(payload, dict):
        raise EasyAuthProtocolError("EasyAuth 响应必须是 JSON 对象")
    return payload


class EasyAuthHttp:
    """在已配置 authority 上发请求:显式超时、禁止 env 代理、限制跳转与响应大小。"""

    def __init__(
        self,
        credential: EasyAuthCredential,
        *,
        timeout: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolve_bearer_token(credential)
        self.credential = credential
        self.timeout = timeout
        self._request = create_trusted_authority_transport(credential.base_url, transport=transport)

    def request(self, method: str, resource_path: str, **kwargs: Any) -> httpx.Response:
        headers = httpx.Headers(kwargs.pop("headers", None))
        headers["Authorization"] = f"Bearer {resolve_bearer_token(self.credential)}"
        headers.setdefault("Accept", "application/json")
        url = app_resource_url(self.credential, resource_path)
        try:
            return self._request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except (httpx.RequestError, UnsafeOutboundUrlError) as exc:
            raise EasyAuthTransportError("EasyAuth 请求失败") from exc
