"""共享集成 URL 校验。"""

from urllib.parse import urlsplit

from enterprise_platform.safe_http import UnsafeOutboundUrlError, validate_outbound_url

DEV_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_endpoint_url(value: str) -> str:
    text = value.strip()
    if not text:
        return value
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("必须是 http(s) 绝对地址")
    if parsed.username or parsed.password:
        raise ValueError("地址不得包含 userinfo")
    if scheme == "http" and parsed.hostname.lower() not in DEV_HTTP_HOSTS:
        raise ValueError("非本机地址必须使用 https")
    return value


def validate_permission_request_url(value: str) -> str:
    text = value.strip()
    if text.startswith("/") and (text.startswith("//") or "\\" in text):
        raise ValueError("权限申请页站内路径无效")
    if text and not text.startswith(("http://", "https://", "/")):
        raise ValueError("权限申请页 URL 必须是 http(s) 绝对地址或站内路径")
    if text.startswith(("http://", "https://")):
        validate_endpoint_url(text)
    return text


def validate_server_base_url(value: str) -> str:
    """校验显式受信的 server-side authority；允许内网 HTTP，但拒绝歧义 URL。"""

    text = value.strip()
    if not text:
        return value
    try:
        parsed = urlsplit(text)
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("serverBaseUrl 必须是合法的 http(s) 绝对 authority") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("serverBaseUrl 必须是 http(s) 绝对 authority")
    if parsed.username or parsed.password:
        raise ValueError("serverBaseUrl 不得包含 userinfo")
    if parsed.fragment:
        raise ValueError("serverBaseUrl 不得包含 fragment")
    if parsed.query or parsed.path not in {"", "/"}:
        raise ValueError("serverBaseUrl 只能包含 scheme、host 与 port")
    return value


__all__ = [
    "UnsafeOutboundUrlError",
    "validate_endpoint_url",
    "validate_outbound_url",
    "validate_permission_request_url",
    "validate_server_base_url",
]
