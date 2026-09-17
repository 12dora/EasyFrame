"""每 HTTP 请求一个可变 memo。contextvars 会拷进线程池,只改 dict。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar, Token
from typing import Any

_Scope = MutableMapping[str, Any]
_request_scope: ContextVar[dict[str, Any] | None] = ContextVar("enterprise_request_scope", default=None)

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[None]], Callable[..., Awaitable[None]]], Awaitable[None]]


def begin_request_scope() -> Token[dict[str, Any] | None]:
    return _request_scope.set({})


def end_request_scope(token: Token[dict[str, Any] | None]) -> None:
    _request_scope.reset(token)


def request_scope() -> dict[str, Any] | None:
    """无请求(调度线程、直接调适配器)时为 None,调用方不得 memo。"""

    return _request_scope.get()


def drop_request_memo(*keys: str) -> None:
    """安全突变后丢掉请求内指定 memo,避免同一请求读到吊销前快照。"""

    memo = request_scope()
    if memo is None:
        return
    for key in keys:
        memo.pop(key, None)


class RequestScopeMiddleware:
    """纯 ASGI,不用 BaseHTTPMiddleware,避免再拷一层 context。

    宿主须在文件里所有 ``@app.middleware("http")`` 声明之后
    ``app.add_middleware(RequestScopeMiddleware)``,让它包在 BaseHTTPMiddleware 之外。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = begin_request_scope()
        try:
            await self.app(scope, receive, send)
        finally:
            end_request_scope(token)
