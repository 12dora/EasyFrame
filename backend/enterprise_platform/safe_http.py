"""共享服务端 HTTP 出站防护：URL、DNS/IP、逐跳重定向与响应上限。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class UnsafeOutboundUrlError(ValueError):
    pass


REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 512 * 1024
DEV_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_outbound_url(url: str, *, allow_localhost: bool = False) -> str:
    _validated_addresses(url, allow_localhost=allow_localhost)
    return url


def _validated_host(parsed, *, allow_localhost: bool) -> str:
    """URL 结构、userinfo、https 与 localhost 守卫;通过后返回主机名。"""

    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsafeOutboundUrlError("必须是 http(s) 绝对地址")
    if parsed.username or parsed.password:
        raise UnsafeOutboundUrlError("出站地址不得包含 userinfo")
    if parsed.scheme == "http" and not (allow_localhost and host.lower() in DEV_HTTP_HOSTS):
        raise UnsafeOutboundUrlError("出站地址必须使用 https")
    if host.lower() == "localhost" and not allow_localhost:
        raise UnsafeOutboundUrlError("目标为本机/内网/保留地址,已拒绝")
    return host


def _guard_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool, detail: str
) -> None:
    if _blocked(address) and not (allow_loopback and address.is_loopback):
        raise UnsafeOutboundUrlError(detail)


def _resolve(parsed, host: str) -> list:
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeOutboundUrlError("无法解析目标主机") from exc
    if not infos:
        raise UnsafeOutboundUrlError("无法解析目标主机")
    return infos


def _validated_addresses(url: str, *, allow_localhost: bool) -> tuple[str, ...]:
    parsed = urlsplit(url.strip())
    host = _validated_host(parsed, allow_localhost=allow_localhost)
    literal = _ip(host)
    if literal is not None:
        _guard_address(literal, allow_loopback=allow_localhost, detail="目标为本机/内网/保留地址,已拒绝")
        return (str(literal),)
    allow_loopback = allow_localhost and host.lower() == "localhost"
    addresses: list[str] = []
    for info in _resolve(parsed, host):
        address = ipaddress.ip_address(info[4][0])
        _guard_address(address, allow_loopback=allow_loopback, detail="目标解析到本机/内网/保留地址,已拒绝")
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    return tuple(addresses)


def guarded_request(
    method: str,
    url: str,
    *,
    timeout: float = 5,
    allow_localhost: bool = False,
    max_body_bytes: int = MAX_BODY_BYTES,
    **kwargs,
) -> httpx.Response:
    current = url
    request_method = method.upper()
    for _ in range(MAX_REDIRECTS + 1):
        pinned_url, host_header, sni_hostname = _pinned_target(current, allow_localhost=allow_localhost)
        request_kwargs = dict(kwargs)
        headers = httpx.Headers(request_kwargs.pop("headers", None))
        headers["Host"] = host_header
        extensions = dict(request_kwargs.pop("extensions", {}))
        extensions["sni_hostname"] = sni_hostname
        if request_kwargs.pop("proxy", None) is not None:
            raise UnsafeOutboundUrlError("固定目标请求不允许单独配置代理")
        request_kwargs.pop("trust_env", None)
        verify = request_kwargs.pop("verify", True)
        with httpx.Client(verify=verify, trust_env=False) as client:
            with client.stream(
                request_method,
                pinned_url,
                headers=headers,
                extensions=extensions,
                timeout=timeout,
                follow_redirects=False,
                **request_kwargs,
            ) as response:
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        return _materialize(response, max_body_bytes)
                    redirected = urljoin(current, location)
                    if _authority(current) != _authority(redirected):
                        raise UnsafeOutboundUrlError("出站重定向不得切换协议、主机或端口")
                    current = redirected
                    if response.status_code == 303:
                        request_method = "GET"
                        kwargs.pop("data", None)
                        kwargs.pop("json", None)
                    continue
                return _materialize(response, max_body_bytes)
    raise UnsafeOutboundUrlError("重定向次数过多")


def _materialize(response: httpx.Response, max_body_bytes: int) -> httpx.Response:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_body_bytes:
        raise UnsafeOutboundUrlError("上游响应过大")
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > max_body_bytes:
            raise UnsafeOutboundUrlError("上游响应过大")
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=bytes(content),
        request=response.request,
        extensions=response.extensions,
    )


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _blocked(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_reserved
        or value.is_unspecified
        or value.is_multicast
    )


def _authority(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port


def _pinned_target(url: str, *, allow_localhost: bool) -> tuple[str, str, str]:
    """把请求连接固定到已校验 IP，同时保留原 Host 与 TLS SNI。"""

    parsed = urlsplit(url.strip())
    host = parsed.hostname
    if host is None:
        raise UnsafeOutboundUrlError("必须是 http(s) 绝对地址")
    addresses = _validated_addresses(url, allow_localhost=allow_localhost)
    if not addresses:
        raise UnsafeOutboundUrlError("无法解析目标主机")
    address = addresses[0]
    pinned_host = f"[{address}]" if _ip(address) and _ip(address).version == 6 else address
    pinned_netloc = f"{pinned_host}:{parsed.port}" if parsed.port is not None else pinned_host
    original_host = host.encode("idna").decode("ascii")
    rendered_host = f"[{original_host}]" if _ip(original_host) and _ip(original_host).version == 6 else original_host
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = f"{rendered_host}:{parsed.port}" if parsed.port not in {None, default_port} else rendered_host
    return (
        urlunsplit((parsed.scheme, pinned_netloc, parsed.path, parsed.query, parsed.fragment)),
        host_header,
        original_host,
    )
