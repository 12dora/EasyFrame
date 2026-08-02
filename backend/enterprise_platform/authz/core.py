"""宿主无关的 EasyAuth grants 合同与 fail-closed 权限归一化。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class DataScope(StrEnum):
    """企业应用统一支持的数据范围。"""

    SELF = "SELF"
    MANAGED_USERS = "MANAGED_USERS"
    ALL = "ALL"


SCOPE_ORDER = {DataScope.SELF: 1, DataScope.MANAGED_USERS: 2, DataScope.ALL: 3}


class UnsupportedDataScopeError(ValueError):
    """scope 不在共享白名单中；调用方必须安全拒绝。"""


def parse_data_scope(value: str | DataScope) -> DataScope:
    """严格解析 scope，不接受历史别名或未知值。"""

    try:
        return DataScope(str(value).upper())
    except ValueError as exc:
        raise UnsupportedDataScopeError(f"unsupported data scope: {value}") from exc


class EasyAuthGrantResolved(BaseModel):
    """EasyAuth 展开的 MANAGED_USERS 集合。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    user_ids: list[str] = Field(default_factory=list)
    resolver: str | None = None
    resolved_at: datetime | None = None
    snapshot_version: str | None = None
    expires_at: datetime | None = None


class EasyAuthGrantItem(BaseModel):
    """EasyAuth permission snapshot 中的一条 grant。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    permission: str
    scope: str
    source_type: str
    source_key: str
    resolved: EasyAuthGrantResolved | None = None

    @property
    def source(self) -> str:
        if self.source_type == "group" and self.source_key:
            return f"group:{self.source_key}"
        return self.source_type


class EasyAuthGroupItem(BaseModel):
    """EasyAuth permission snapshot 中的授权组摘要。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    key: str
    kind: str
    name: str


class EasyAuthPermissionSnapshot(BaseModel):
    """所有企业应用共同消费的 EasyAuth permission snapshot 合同。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    user_id: str
    app_key: str
    groups: list[EasyAuthGroupItem] = Field(default_factory=list)
    grants: list[EasyAuthGrantItem]
    grant_version: int
    catalog_version: int
    snapshot_version: str
    expires_at: datetime

    @field_validator("grants", mode="before")
    @classmethod
    def isolate_malformed_grants(cls, value: Any) -> tuple[EasyAuthGrantItem, ...]:
        """上游单条坏 grant 不得使整个 snapshot 失效。"""

        if not isinstance(value, list | tuple):
            raise ValueError("grants must be a list")
        return parse_grants_isolated(value)


@dataclass(frozen=True, slots=True)
class CatalogPermission:
    """授权计算所需的最小本地权限目录投影。"""

    code: str
    supported_scopes: frozenset[DataScope]
    active: bool = True


@dataclass(frozen=True, slots=True)
class NormalizedGrant:
    """逐条通过合同、目录和 scope 校验后的授权事实。"""

    permission_code: str
    data_scope: DataScope
    source: str | None = None
    resolved: EasyAuthGrantResolved | None = None


SkipCallback = Callable[[str, str | None], None]


def parse_grants_isolated(
    grants: Iterable[EasyAuthGrantItem | Mapping[str, Any]],
    *,
    on_skip: SkipCallback | None = None,
) -> tuple[EasyAuthGrantItem, ...]:
    """逐条解析上游 grant；一条 malformed 不会毒化同一 snapshot。"""

    parsed: list[EasyAuthGrantItem] = []
    for raw in grants:
        try:
            parsed.append(raw if isinstance(raw, EasyAuthGrantItem) else EasyAuthGrantItem.model_validate(raw))
        except (TypeError, ValidationError):
            if on_skip is not None:
                on_skip("malformed", None)
            else:
                # Snapshot schema 的 field validator 没有宿主 callback 可注入；
                # 仍必须留下安全告警，且绝不记录原始 grant/外部用户标识。
                logger.warning("easyauth malformed grant skipped")
    return tuple(parsed)


def normalize_grants(
    grants: Iterable[EasyAuthGrantItem | Mapping[str, Any]],
    catalog: Mapping[str, CatalogPermission],
    *,
    on_skip: SkipCallback | None = None,
) -> tuple[NormalizedGrant, ...]:
    """逐条隔离坏 grant；未知、停用、越界 scope 一律跳过。"""

    normalized: list[NormalizedGrant] = []

    def skip(reason: str, permission: str | None = None) -> None:
        if on_skip is not None:
            on_skip(reason, permission)

    for grant in parse_grants_isolated(grants, on_skip=on_skip):
        permission = catalog.get(grant.permission)
        if permission is None:
            skip("unknown_permission", grant.permission)
            continue
        if not permission.active:
            skip("inactive_permission", grant.permission)
            continue
        try:
            scope = parse_data_scope(grant.scope)
        except UnsupportedDataScopeError:
            skip("invalid_scope", grant.permission)
            continue
        if scope not in permission.supported_scopes:
            skip("unsupported_scope", grant.permission)
            continue
        if scope is DataScope.MANAGED_USERS and grant.resolved is None:
            skip("missing_resolved_users", grant.permission)
            continue
        normalized.append(NormalizedGrant(grant.permission, scope, grant.source, grant.resolved))
    return tuple(normalized)


class ScopeGrant(Protocol):
    permission_code: str
    data_scope: Any


def best_scope_for_grants(grants: Iterable[ScopeGrant], permission_code: str) -> DataScope | None:
    """返回同一 permission 的最高合法 scope；未知 scope 安全拒绝。"""

    best: DataScope | None = None
    for grant in grants:
        if grant.permission_code != permission_code:
            continue
        scope = parse_data_scope(str(grant.data_scope))
        if best is None or SCOPE_ORDER[scope] > SCOPE_ORDER[best]:
            best = scope
    return best


Subject = TypeVar("Subject")


def subject_ids_for_scope(
    scope: str | DataScope,
    *,
    current_subject_id: Subject,
    managed_subject_ids: Iterable[Subject] = (),
) -> set[Subject] | None:
    """解析单个 scope；ALL 用 None 表示不过滤，MANAGED_USERS 不含本人。"""

    parsed_scope = parse_data_scope(scope)
    if parsed_scope is DataScope.ALL:
        return None
    if parsed_scope is DataScope.SELF:
        return {current_subject_id}
    return set(managed_subject_ids) - {current_subject_id}


def union_subject_ids_for_permission(
    grants: Iterable[ScopeGrant],
    permission_code: str,
    *,
    current_subject_id: Subject,
    managed_subject_ids: Callable[[ScopeGrant], Iterable[Subject]],
) -> set[Subject] | None:
    """按同一 permission grants 并集解析 subject；任一 ALL 直接不过滤。"""

    allowed: set[Subject] = set()
    for grant in grants:
        if grant.permission_code != permission_code:
            continue
        scope = parse_data_scope(str(grant.data_scope))
        subjects = subject_ids_for_scope(
            scope,
            current_subject_id=current_subject_id,
            managed_subject_ids=managed_subject_ids(grant) if scope is DataScope.MANAGED_USERS else (),
        )
        if subjects is None:
            return None
        allowed.update(subjects)
    return allowed


def allowed_external_user_ids(
    grants: Iterable[NormalizedGrant],
    permission_code: str,
    *,
    current_external_user_id: str,
) -> set[str] | None:
    """直接从 normalized grants 解析外部用户集合。"""

    return union_subject_ids_for_permission(
        grants,
        permission_code,
        current_subject_id=current_external_user_id,
        managed_subject_ids=lambda grant: grant.resolved.user_ids if grant.resolved is not None else (),
    )
