"""EasyAuth DirectoryClient:假 transport,无网络、无数据库。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from enterprise_platform.easyauth import (
    DirectoryAccessError,
    DirectoryClient,
    DirectoryInconsistentSnapshotError,
    DirectorySnapshotDriftError,
    EasyAuthCredential,
    EasyAuthCredentialError,
)

BASE_URL = "https://easyauth.example.test"
APP_KEY = "easytrade"
TOKEN = "eat_directory_test"
EMAIL = "xiaoming@example.com"
MOBILE = "13800000000"
USER_A = "dt:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:dXNlcjAxMjM"
USER_B = "dt:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:dXNlcjA0NTY"


def _credential() -> EasyAuthCredential:
    return EasyAuthCredential(
        base_url=BASE_URL,
        app_key=APP_KEY,
        auth_mode="static_app_token",
        credential=TOKEN,
    )


def _client(handler) -> DirectoryClient:
    return DirectoryClient(_credential(), transport=httpx.MockTransport(handler))


def _scope() -> dict[str, Any]:
    return {
        "source_slug": "dingtalk",
        "corp_id": "corp-demo",
        "generation": 42,
        "status": "success",
        "snapshot_at": "2026-07-16T10:00:00+08:00",
        "snapshot_at_status": "valid",
        "stale": False,
    }


def _snapshot(*, snapshot_id: str = "snap-1") -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "snapshots": [_scope()],
        "stale": False,
        "complete": True,
        "authoritative": True,
    }


def _user(*, user_ref: str, user_id: str | None = "f7c31a09e5b24f8d9a1c") -> dict[str, Any]:
    return {
        "user_id": user_id,
        "dingtalk_user_id": "user0123",
        "source_slug": "dingtalk",
        "corp_id": "corp-demo",
        "user_ref": user_ref,
        "name": "李小明",
        "title": "后端工程师",
        "email": EMAIL,
        "mobile": MOBILE,
        "employee_number": "ET-00123",
        "status": "active",
        "departments": [
            {
                "department_id": "460001",
                "source_slug": "dingtalk",
                "corp_id": "corp-demo",
                "department_ref": "dept:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:NDYwMDAx",
                "name": "研发部",
            }
        ],
        "active": True,
        "manager": None,
    }


def _page(
    *,
    users: list[dict[str, Any]],
    page: int,
    page_size: int,
    total_items: int,
    total_pages: int,
    snapshot_id: str = "snap-1",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": users,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
            },
            "directory_snapshot": _snapshot(snapshot_id=snapshot_id),
        },
    )


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "denied", "details": {}}})


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query, keep_blank_values=True)


def test_read_full_snapshot_pages_and_pins_snapshot_id() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == f"/api/v1/apps/{APP_KEY}/directory/users"
        query = _query(request)
        seen.append(query)
        page = int(query["page"][0])
        assert query["page_size"] == ["1"]
        assert query["include_inactive"] == ["true"]
        if page == 1:
            assert "snapshot_id" not in query
            return _page(users=[_user(user_ref=USER_A)], page=1, page_size=1, total_items=2, total_pages=2)
        assert page == 2
        assert query["snapshot_id"] == ["snap-1"]
        return _page(
            users=[_user(user_ref=USER_B, user_id=None)],
            page=2,
            page_size=1,
            total_items=2,
            total_pages=2,
        )

    result = _client(handler).read_full_snapshot(page_size=1)
    assert [user.user_ref for user in result.users] == [USER_A, USER_B]
    assert result.users[1].user_id is None
    assert result.users[0].department_refs == ("dept:v1:ZGluZ3RhbGs:Y29ycC1kZW1v:NDYwMDAx",)
    assert result.snapshot.snapshot_id == "snap-1"
    assert result.snapshot.authoritative is True
    assert result.snapshot.scopes[0].corp_id == "corp-demo"
    assert "snapshot_id" not in seen[0]
    assert seen[1]["snapshot_id"] == ["snap-1"]


def test_read_full_snapshot_restarts_after_409_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        query = _query(request)
        page = int(query["page"][0])
        snapshot_id = query.get("snapshot_id", [None])[0]
        if calls == 1:
            assert snapshot_id is None and page == 1
            return _page(
                users=[_user(user_ref=USER_A)],
                page=1,
                page_size=1,
                total_items=2,
                total_pages=2,
                snapshot_id="snap-old",
            )
        if calls == 2:
            assert snapshot_id == "snap-old" and page == 2
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "CONFLICT",
                        "message": "目录快照已变化",
                        "details": {"reason": "snapshot_changed"},
                    }
                },
            )
        if calls == 3:
            assert snapshot_id is None and page == 1
            return _page(
                users=[_user(user_ref=USER_A)],
                page=1,
                page_size=1,
                total_items=2,
                total_pages=2,
                snapshot_id="snap-new",
            )
        assert snapshot_id == "snap-new" and page == 2
        return _page(
            users=[_user(user_ref=USER_B, user_id=None)],
            page=2,
            page_size=1,
            total_items=2,
            total_pages=2,
            snapshot_id="snap-new",
        )

    result = _client(handler).read_full_snapshot(page_size=1)
    assert calls == 4
    assert [user.user_ref for user in result.users] == [USER_A, USER_B]
    assert result.snapshot.snapshot_id == "snap-new"


def test_read_full_snapshot_raises_when_restart_budget_exhausted() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if _query(request).get("snapshot_id"):
            return httpx.Response(409, json={"error": {"code": "CONFLICT", "message": "changed", "details": {}}})
        return _page(users=[_user(user_ref=USER_A)], page=1, page_size=1, total_items=2, total_pages=2)

    with pytest.raises(DirectorySnapshotDriftError, match="重启次数已用尽"):
        _client(handler).read_full_snapshot(page_size=1, max_restarts=1)
    # 首页 + 第二页 409,重启一次后再首页 + 第二页 409
    assert calls == 4


def test_read_full_snapshot_detects_duplicate_user_ref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _page(
            users=[_user(user_ref=USER_A), _user(user_ref=USER_A)],
            page=1,
            page_size=200,
            total_items=2,
            total_pages=1,
        )

    with pytest.raises(DirectoryInconsistentSnapshotError, match="重复 user_ref"):
        _client(handler).read_full_snapshot()


def test_read_full_snapshot_detects_total_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _page(users=[_user(user_ref=USER_A)], page=1, page_size=200, total_items=2, total_pages=1)

    with pytest.raises(DirectoryInconsistentSnapshotError, match="total_items"):
        _client(handler).read_full_snapshot()


@pytest.mark.parametrize(("status", "code"), [(401, "AUTHENTICATION_FAILED"), (403, "PERMISSION_DENIED")])
def test_read_full_snapshot_maps_access_errors(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _error(status, code)

    with pytest.raises(DirectoryAccessError) as captured:
        _client(handler).read_full_snapshot()
    assert captured.value.status == status
    assert captured.value.code == code


def test_oauth_client_credentials_fails_closed_without_token_provider() -> None:
    credential = EasyAuthCredential(
        base_url=BASE_URL,
        app_key=APP_KEY,
        auth_mode="oauth_client_credentials",
        credential="oauth-secret",
    )
    with pytest.raises(EasyAuthCredentialError, match="OAuth token provider 未配置"):
        DirectoryClient(credential, transport=httpx.MockTransport(lambda _request: httpx.Response(500)))


def test_directory_client_does_not_put_email_or_mobile_in_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _page(
            users=[_user(user_ref=USER_A), _user(user_ref=USER_A)],
            page=1,
            page_size=200,
            total_items=2,
            total_pages=1,
        )

    with pytest.raises(DirectoryInconsistentSnapshotError) as captured:
        _client(handler).read_full_snapshot()
    text = str(captured.value)
    assert EMAIL not in text
    assert MOBILE not in text
    assert USER_A in text
