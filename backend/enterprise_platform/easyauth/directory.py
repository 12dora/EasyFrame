"""EasyAuth 用户目录全量快照读取。不记录 email/mobile。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from enterprise_platform.easyauth.credentials import (
    EasyAuthCredential,
    EasyAuthHttp,
    EasyAuthProtocolError,
    EasyAuthTransportError,
    error_code_of,
    response_object,
)

_USER_STATUSES = frozenset({"active", "disabled", "departed"})
_MAX_PAGE_SIZE = 200


class DirectoryClientError(RuntimeError):
    pass


class DirectoryAccessError(DirectoryClientError):
    """401/403:凭据无效或未开通 directory 能力,不得重试。"""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or f"EasyAuth directory 拒绝访问 HTTP {status} {code}".strip())
        self.status = status
        self.code = code


class DirectoryUnavailableError(DirectoryClientError):
    """网络 / 5xx / 429:可稍后重试。"""


class DirectorySnapshotDriftError(DirectoryClientError):
    """分页期间快照变化,且已用尽重启次数。"""


class DirectoryInconsistentSnapshotError(DirectoryClientError):
    """单次读取内部不一致(重复 user_ref、total 对不上、snapshot_id 漂移)。"""


class _SnapshotConflictError(Exception):
    """服务端 409:本轮页必须丢弃,由调用方决定是否重启。"""


@dataclass(frozen=True)
class DirectoryUserRecord:
    user_ref: str
    user_id: str | None
    source_slug: str
    corp_id: str
    dingtalk_user_id: str
    name: str
    email: str
    mobile: str
    employee_number: str
    title: str
    status: str
    active: bool
    department_refs: tuple[str, ...]


@dataclass(frozen=True)
class DirectorySnapshotScope:
    source_slug: str
    corp_id: str
    generation: int
    status: str
    snapshot_at: str
    snapshot_at_status: str
    stale: bool


@dataclass(frozen=True)
class DirectorySnapshotMeta:
    snapshot_id: str
    complete: bool
    stale: bool
    authoritative: bool
    scopes: tuple[DirectorySnapshotScope, ...]


@dataclass(frozen=True)
class DirectorySnapshotRead:
    users: tuple[DirectoryUserRecord, ...]
    snapshot: DirectorySnapshotMeta


@dataclass(frozen=True)
class _Pagination:
    page: int
    page_size: int
    total_items: int
    total_pages: int


class DirectoryClient:
    def __init__(
        self,
        credential: EasyAuthCredential,
        *,
        timeout: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = EasyAuthHttp(credential, timeout=timeout, transport=transport)

    def read_full_snapshot(
        self,
        *,
        include_inactive: bool = True,
        page_size: int = 200,
        max_restarts: int = 3,
    ) -> DirectorySnapshotRead:
        if page_size < 1 or page_size > _MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须在 1..{_MAX_PAGE_SIZE}")
        if max_restarts < 0:
            raise ValueError("max_restarts 不能为负")
        restarts = 0
        while True:
            try:
                return self._read_once(include_inactive=include_inactive, page_size=page_size)
            except _SnapshotConflictError as exc:
                if restarts >= max_restarts:
                    raise DirectorySnapshotDriftError("EasyAuth directory 快照在分页期间变化,重启次数已用尽") from exc
                restarts += 1

    def _read_once(self, *, include_inactive: bool, page_size: int) -> DirectorySnapshotRead:
        users: list[DirectoryUserRecord] = []
        seen_refs: set[str] = set()
        pinned_id: str | None = None
        expected_total: int | None = None
        expected_pages: int | None = None
        snapshot: DirectorySnapshotMeta | None = None
        page = 1
        while True:
            payload = self._fetch_page(
                page=page,
                page_size=page_size,
                include_inactive=include_inactive,
                snapshot_id=pinned_id,
            )
            pagination = _parse_pagination(payload)
            meta = _parse_snapshot_meta(payload)
            pinned_id, expected_total, expected_pages, snapshot = _bind_or_check_pin(
                page,
                pinned_id,
                expected_total,
                expected_pages,
                snapshot,
                pagination,
                meta,
            )
            for item in _parse_data(payload):
                user = _parse_user(item)
                if user.user_ref in seen_refs:
                    raise DirectoryInconsistentSnapshotError(f"directory 快照出现重复 user_ref:{user.user_ref}")
                seen_refs.add(user.user_ref)
                users.append(user)
            if page >= expected_pages:
                break
            page += 1
        if len(users) != expected_total:
            raise DirectoryInconsistentSnapshotError(
                f"directory 快照人数与 total_items 不一致:实际 {len(users)} 声明 {expected_total}"
            )
        if snapshot is None:
            raise DirectoryInconsistentSnapshotError("directory 快照缺少 directory_snapshot")
        return DirectorySnapshotRead(users=tuple(users), snapshot=snapshot)

    def _fetch_page(
        self,
        *,
        page: int,
        page_size: int,
        include_inactive: bool,
        snapshot_id: str | None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"page": page, "page_size": page_size}
        if include_inactive:
            params["include_inactive"] = "true"
        if snapshot_id:
            params["snapshot_id"] = snapshot_id
        try:
            response = self._http.request("GET", "/directory/users", params=params)
        except EasyAuthTransportError as exc:
            raise DirectoryUnavailableError("EasyAuth directory 不可达") from exc
        return _map_directory_response(response)


def _map_directory_response(response: httpx.Response) -> dict[str, Any]:
    status = response.status_code
    if status == 200:
        try:
            return response_object(response)
        except EasyAuthProtocolError as exc:
            raise DirectoryInconsistentSnapshotError("EasyAuth directory 响应不是 JSON 对象") from exc
    if status == 409:
        raise _SnapshotConflictError()
    if status in {401, 403}:
        raise DirectoryAccessError(status, error_code_of(response))
    if status == 429 or status >= 500:
        raise DirectoryUnavailableError(f"EasyAuth directory 返回 HTTP {status}")
    if status >= 400:
        raise DirectoryClientError(f"EasyAuth directory 返回 HTTP {status}")
    raise DirectoryUnavailableError(f"EasyAuth directory 返回 HTTP {status}")


def _bind_or_check_pin(
    page: int,
    pinned_id: str | None,
    expected_total: int | None,
    expected_pages: int | None,
    snapshot: DirectorySnapshotMeta | None,
    pagination: _Pagination,
    meta: DirectorySnapshotMeta,
) -> tuple[str, int, int, DirectorySnapshotMeta]:
    if pagination.page != page:
        raise DirectoryInconsistentSnapshotError("directory pagination.page 与请求页码不一致")
    if pinned_id is None:
        return meta.snapshot_id, pagination.total_items, pagination.total_pages, meta
    if expected_total is None or expected_pages is None or snapshot is None:
        raise DirectoryInconsistentSnapshotError("directory 快照缺少 directory_snapshot")
    if meta.snapshot_id != pinned_id:
        raise DirectoryInconsistentSnapshotError("directory 后续页 snapshot_id 与首页不一致")
    if pagination.total_items != expected_total or pagination.total_pages != expected_pages:
        raise DirectoryInconsistentSnapshotError("directory 后续页 pagination 与首页不一致")
    return pinned_id, expected_total, expected_pages, snapshot


def _parse_data(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise DirectoryInconsistentSnapshotError("directory 响应缺少 data 数组")
    return data


def _parse_pagination(payload: dict[str, Any]) -> _Pagination:
    block = payload.get("pagination")
    if not isinstance(block, dict):
        raise DirectoryInconsistentSnapshotError("directory 响应缺少 pagination")
    page = _require_int(block.get("page"), "pagination.page")
    page_size = _require_int(block.get("page_size"), "pagination.page_size")
    total_items = _require_int(block.get("total_items"), "pagination.total_items")
    total_pages = _require_int(block.get("total_pages"), "pagination.total_pages")
    if page < 1 or page_size < 1 or total_items < 0 or total_pages < 0:
        raise DirectoryInconsistentSnapshotError("directory pagination 数值非法")
    if total_pages == 0 and total_items != 0:
        raise DirectoryInconsistentSnapshotError("directory total_pages=0 但 total_items 非 0")
    if total_items > 0 and total_pages < 1:
        raise DirectoryInconsistentSnapshotError("directory total_pages 非法")
    return _Pagination(page=page, page_size=page_size, total_items=total_items, total_pages=total_pages)


def _parse_snapshot_meta(payload: dict[str, Any]) -> DirectorySnapshotMeta:
    block = payload.get("directory_snapshot")
    if not isinstance(block, dict):
        raise DirectoryInconsistentSnapshotError("directory 响应缺少 directory_snapshot")
    snapshot_id = _require_str(block.get("snapshot_id"), "directory_snapshot.snapshot_id")
    if not snapshot_id:
        raise DirectoryInconsistentSnapshotError("directory_snapshot.snapshot_id 不能为空")
    complete = _require_bool(block.get("complete"), "directory_snapshot.complete")
    stale = _require_bool(block.get("stale"), "directory_snapshot.stale")
    authoritative = _require_bool(block.get("authoritative"), "directory_snapshot.authoritative")
    raw_scopes = block.get("snapshots")
    if not isinstance(raw_scopes, list):
        raise DirectoryInconsistentSnapshotError("directory_snapshot.snapshots 必须是数组")
    scopes = tuple(_parse_scope(item) for item in raw_scopes)
    return DirectorySnapshotMeta(
        snapshot_id=snapshot_id,
        complete=complete,
        stale=stale,
        authoritative=authoritative,
        scopes=scopes,
    )


def _parse_scope(item: Any) -> DirectorySnapshotScope:
    if not isinstance(item, dict):
        raise DirectoryInconsistentSnapshotError("directory_snapshot.snapshots 条目必须是对象")
    return DirectorySnapshotScope(
        source_slug=_require_str(item.get("source_slug"), "snapshots.source_slug"),
        corp_id=_require_str(item.get("corp_id"), "snapshots.corp_id"),
        generation=_require_int(item.get("generation"), "snapshots.generation"),
        status=_require_str(item.get("status"), "snapshots.status"),
        snapshot_at=_require_str(item.get("snapshot_at"), "snapshots.snapshot_at"),
        snapshot_at_status=_require_str(item.get("snapshot_at_status"), "snapshots.snapshot_at_status"),
        stale=_require_bool(item.get("stale"), "snapshots.stale"),
    )


def _parse_user(item: Any) -> DirectoryUserRecord:
    if not isinstance(item, dict):
        raise DirectoryInconsistentSnapshotError("directory 用户条目必须是对象")
    user_ref = _require_str(item.get("user_ref"), "user_ref")
    if not user_ref:
        raise DirectoryInconsistentSnapshotError("user_ref 不能为空")
    status = _require_str(item.get("status"), "status")
    if status not in _USER_STATUSES:
        raise DirectoryInconsistentSnapshotError(f"非法的 directory status:{status}")
    return DirectoryUserRecord(
        user_ref=user_ref,
        user_id=_optional_user_id(item.get("user_id")),
        source_slug=_require_str(item.get("source_slug"), "source_slug"),
        corp_id=_require_str(item.get("corp_id"), "corp_id"),
        dingtalk_user_id=_require_str(item.get("dingtalk_user_id"), "dingtalk_user_id"),
        name=_require_str(item.get("name"), "name"),
        email=_require_str(item.get("email"), "email"),
        mobile=_require_str(item.get("mobile"), "mobile"),
        employee_number=_require_str(item.get("employee_number"), "employee_number"),
        title=_optional_title(item.get("title")),
        status=status,
        active=_require_bool(item.get("active"), "active"),
        department_refs=_parse_department_refs(item.get("departments")),
    )


def _parse_department_refs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DirectoryInconsistentSnapshotError("departments 必须是数组")
    refs: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise DirectoryInconsistentSnapshotError("departments 条目必须是对象")
        ref = _require_str(entry.get("department_ref"), "department_ref")
        if not ref:
            raise DirectoryInconsistentSnapshotError("department_ref 不能为空")
        refs.append(ref)
    return tuple(refs)


def _optional_user_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DirectoryInconsistentSnapshotError("user_id 必须是字符串或 null")
    return value


def _optional_title(value: Any) -> str:
    if value is None:
        return ""
    return _require_str(value, "title")


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DirectoryInconsistentSnapshotError(f"{field} 必须是字符串")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DirectoryInconsistentSnapshotError(f"{field} 必须是布尔值")
    return value


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectoryInconsistentSnapshotError(f"{field} 必须是整数")
    return value
