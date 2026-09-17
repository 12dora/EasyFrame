"""请求级 memo:HTTP 开启、非 HTTP 透传、线程池只改 dict。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from enterprise_platform.request_scope import (
    RequestScopeMiddleware,
    begin_request_scope,
    drop_request_memo,
    end_request_scope,
    request_scope,
)


def test_request_scope_is_none_outside_http() -> None:
    assert request_scope() is None
    drop_request_memo("current_user")


def test_begin_end_yields_fresh_mutable_dict() -> None:
    token = begin_request_scope()
    try:
        memo = request_scope()
        assert memo == {}
        memo["current_user"] = "alice"
        drop_request_memo("current_user", "missing")
        assert request_scope() == {}
    finally:
        end_request_scope(token)
    assert request_scope() is None


def test_http_middleware_opens_and_clears_memo() -> None:
    app = FastAPI()
    observed: list[dict[str, object] | None] = []

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        memo = request_scope()
        assert memo is not None
        memo["seen"] = True
        observed.append(dict(memo))
        return {"ok": True}

    app.add_middleware(RequestScopeMiddleware)
    with TestClient(app) as client:
        assert client.get("/probe").status_code == 200
    assert observed == [{"seen": True}]
    assert request_scope() is None


def test_drop_request_memo_inside_http() -> None:
    app = FastAPI()

    @app.get("/drop")
    def drop() -> dict[str, bool]:
        memo = request_scope()
        assert memo is not None
        memo["current_user"] = object()
        memo["account"] = object()
        drop_request_memo("current_user", "account")
        assert request_scope() == {}
        return {"ok": True}

    app.add_middleware(RequestScopeMiddleware)
    assert TestClient(app).get("/drop").status_code == 200


def test_threadpool_worker_mutates_same_dict() -> None:
    token = begin_request_scope()
    try:
        memo = request_scope()
        assert memo is not None
        memo["n"] = 0
        ctx = copy_context()

        def bump() -> None:
            request_scope()["n"] += 1

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, bump).result(timeout=5)
        assert memo["n"] == 1
        assert request_scope() is memo
    finally:
        end_request_scope(token)


def test_non_http_scope_is_passthrough() -> None:
    seen: dict[str, object] = {}

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        seen["type"] = scope["type"]
        seen["memo"] = request_scope()

    async def run() -> None:
        middleware = RequestScopeMiddleware(inner)
        await middleware({"type": "websocket", "path": "/ws"}, _noop_receive, _noop_send)
        await middleware({"type": "lifespan"}, _noop_receive, _noop_send)

    asyncio.run(run())
    assert seen["type"] == "lifespan"
    assert seen["memo"] is None


async def _noop_receive() -> dict[str, object]:
    return {"type": "websocket.disconnect"}


async def _noop_send(_message: dict[str, object]) -> None:
    return None
