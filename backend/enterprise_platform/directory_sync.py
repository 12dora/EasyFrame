"""将 EasyAuth 目录快照投影到宿主用户表。非权威快照禁止写入。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from enterprise_platform.easyauth.types import (
    DirectorySnapshotDriftError,
    DirectorySnapshotMeta,
    DirectorySnapshotRead,
    DirectoryUserRecord,
)
from enterprise_platform.ports import DirectoryProjectionPort
from enterprise_platform.schemas import DirectorySyncResult, DirectorySyncStatus


class DirectorySnapshotReader(Protocol):
    def read_full_snapshot(self) -> DirectorySnapshotRead: ...


def run_directory_sync(
    reader: DirectorySnapshotReader,
    port: DirectoryProjectionPort,
    *,
    actor_id: str,
    clock: Callable[[], datetime],
) -> DirectorySyncResult:
    """读取全量快照并投影。非权威不写；漂移不写；任何其它异常回滚并标记 failed。"""

    if not actor_id:
        raise ValueError("actor_id 不能为空")
    at = clock()
    try:
        snapshot_read = reader.read_full_snapshot()
    except DirectorySnapshotDriftError as exc:
        return _result(
            status="drift",
            at=at,
            summary="读取目录时快照发生变化，已中止同步，未写入。",
            error_detail=str(exc) or "snapshot_changed",
        )
    except Exception as exc:
        port.rollback()
        return _failed(at, exc)

    users = tuple(snapshot_read.users)
    meta = snapshot_read.snapshot
    if not meta.authoritative:
        unmapped = _unmapped_count(users)
        return _result(
            status="not_authoritative",
            at=at,
            authoritative=meta.authoritative,
            complete=meta.complete,
            stale=meta.stale,
            snapshot_id=meta.snapshot_id,
            upstream_total=len(users),
            unmapped=unmapped,
            summary="目录快照非权威（不完整或已过期），已保留本地用户状态，未写入。",
        )

    try:
        created, updated, unchanged, deactivated, unmapped = _project_authoritative(port, users, meta)
    except Exception as exc:
        port.rollback()
        return _failed(
            at,
            exc,
            authoritative=meta.authoritative,
            complete=meta.complete,
            stale=meta.stale,
            snapshot_id=meta.snapshot_id,
            upstream_total=len(users),
        )

    return _result(
        status="completed",
        at=at,
        authoritative=meta.authoritative,
        complete=meta.complete,
        stale=meta.stale,
        snapshot_id=meta.snapshot_id,
        upstream_total=len(users),
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
        unmapped=unmapped,
        summary=(
            f"目录同步完成：上游 {len(users)} 人，新建 {created}，更新 {updated}，"
            f"未变 {unchanged}，停用 {deactivated}，其中 {unmapped} 人无登录标识。"
        ),
    )


def _project_authoritative(
    port: DirectoryProjectionPort,
    users: Sequence[DirectoryUserRecord],
    meta: DirectorySnapshotMeta,
) -> tuple[int, int, int, int, int]:
    _reject_duplicate_or_empty_refs(users)
    port.begin(meta)
    created = updated = unchanged = unmapped = 0
    seen: list[str] = []
    for user in users:
        outcome = port.upsert_user(user)
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "unchanged":
            unchanged += 1
        else:
            raise ValueError(f"未知的 upsert 结果: {outcome}")
        if user.user_id is None:
            unmapped += 1
        seen.append(user.user_ref)
    deactivated = port.deactivate_missing(frozenset(seen))
    port.commit()
    return created, updated, unchanged, deactivated, unmapped


def _reject_duplicate_or_empty_refs(users: Sequence[DirectoryUserRecord]) -> None:
    seen: set[str] = set()
    for user in users:
        if not user.user_ref:
            raise ValueError("目录用户缺少 user_ref")
        if user.user_ref in seen:
            raise ValueError(f"目录快照含重复 user_ref: {user.user_ref}")
        seen.add(user.user_ref)


def _unmapped_count(users: Sequence[DirectoryUserRecord]) -> int:
    return sum(1 for user in users if user.user_id is None)


def _failed(
    at: datetime,
    exc: BaseException,
    *,
    authoritative: bool = False,
    complete: bool = False,
    stale: bool = False,
    snapshot_id: str = "",
    upstream_total: int = 0,
) -> DirectorySyncResult:
    detail = str(exc) or type(exc).__name__
    return _result(
        status="failed",
        at=at,
        authoritative=authoritative,
        complete=complete,
        stale=stale,
        snapshot_id=snapshot_id,
        upstream_total=upstream_total,
        summary="目录同步失败。",
        error_detail=detail,
    )


def _result(
    *,
    status: DirectorySyncStatus,
    at: datetime,
    summary: str,
    authoritative: bool = False,
    complete: bool = False,
    stale: bool = False,
    snapshot_id: str = "",
    upstream_total: int = 0,
    created: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    deactivated: int = 0,
    unmapped: int = 0,
    error_detail: str | None = None,
) -> DirectorySyncResult:
    return DirectorySyncResult(
        status=status,
        at=at,
        authoritative=authoritative,
        complete=complete,
        stale=stale,
        snapshot_id=snapshot_id,
        upstream_total=upstream_total,
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
        unmapped=unmapped,
        summary=summary,
        error_detail=error_detail,
    )
