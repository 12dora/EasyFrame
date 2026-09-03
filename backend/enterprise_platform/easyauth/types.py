"""EasyAuth 目录快照的共享数据结构。HTTP 客户端与投影内核共用，不含宿主 ORM。"""

from __future__ import annotations

from dataclasses import dataclass


class DirectorySnapshotDriftError(Exception):
    """分页读取期间目录快照变化；调用方必须丢弃已读页。"""


class DirectoryAccessError(Exception):
    """401/403：凭据无效或未开通 directory 能力，不得重试。"""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or f"EasyAuth directory 拒绝访问 HTTP {status} {code}".strip())
        self.status = status
        self.code = code


class DirectoryUnavailableError(Exception):
    """网络 / 5xx / 429：目录暂不可用。"""


@dataclass(frozen=True)
class DirectoryUserRecord:
    """EasyAuth 目录用户条目。``user_ref`` 不透明，原样存储，禁止解析。"""

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
    snapshot_at: str | None
    snapshot_at_status: str
    stale: bool


@dataclass(frozen=True)
class DirectorySnapshotMeta:
    """对应 EasyAuth ``directory_snapshot``。``authoritative`` 仅在 complete 且非 stale 时为真。"""

    snapshot_id: str
    complete: bool
    stale: bool
    authoritative: bool
    scopes: tuple[DirectorySnapshotScope, ...] = ()


@dataclass(frozen=True)
class DirectorySnapshotRead:
    users: tuple[DirectoryUserRecord, ...]
    snapshot: DirectorySnapshotMeta
