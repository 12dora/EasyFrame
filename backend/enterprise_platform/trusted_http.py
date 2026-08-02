"""显式配置 authority 的受信内网 HTTP transport。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from enterprise_platform.safe_http import MAX_BODY_BYTES, MAX_REDIRECTS, REDIRECTS, UnsafeOutboundUrlError


def create_trusted_authority_transport(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Callable[..., httpx.Response]:
    """仅信任配置 authority；允许其解析到容器私网，但禁止跳转到其他 authority。"""

    trusted_authority = _authority(base_url)

    def request(
        method: str,
        url: str,
        *,
        timeout: float = 5,
        max_body_bytes: int = MAX_BODY_BYTES,
        **kwargs,
    ) -> httpx.Response:
        current = url
        request_method = method.upper()
        request_kwargs = dict(kwargs)
        request_kwargs.pop("allow_localhost", None)
        if request_kwargs.pop("proxy", None) is not None:
            raise UnsafeOutboundUrlError("受信 authority 请求不允许单独配置代理")
        request_kwargs.pop("trust_env", None)
        verify = request_kwargs.pop("verify", True)
        for _ in range(MAX_REDIRECTS + 1):
            if _authority(current) != trusted_authority:
                raise UnsafeOutboundUrlError("出站目标不属于已配置的 serverBaseUrl authority")
            with httpx.Client(
                verify=verify,
                trust_env=False,
                follow_redirects=False,
                transport=transport,
            ) as client:
                with client.stream(
                    request_method,
                    current,
                    timeout=timeout,
                    **request_kwargs,
                ) as response:
                    if response.status_code in REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            return _materialize(response, max_body_bytes)
                        redirected = urljoin(current, location)
                        if _authority(redirected) != trusted_authority:
                            raise UnsafeOutboundUrlError("受信 authority 重定向不得切换协议、主机或端口")
                        current = redirected
                        if response.status_code == 303:
                            request_method = "GET"
                            request_kwargs.pop("data", None)
                            request_kwargs.pop("json", None)
                        continue
                    return _materialize(response, max_body_bytes)
        raise UnsafeOutboundUrlError("重定向次数过多")

    return request


def _authority(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeOutboundUrlError("serverBaseUrl 必须是无 userinfo 的 http(s) 绝对地址")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _materialize(response: httpx.Response, max_body_bytes: int) -> httpx.Response:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_body_bytes:
        raise UnsafeOutboundUrlError("上游响应过大")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise UnsafeOutboundUrlError("上游响应过大")
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=bytes(body),
        request=response.request,
        extensions=response.extensions,
    )
