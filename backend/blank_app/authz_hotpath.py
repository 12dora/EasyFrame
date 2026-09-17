"""宿主性能:认证热路径单 Session 解析,近过期/宽限期快照后台刷新。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, object_session

from blank_app.authz_cache import cached_app_key, cached_catalog_floor, catalog_map
from blank_app.database import SessionLocal
from blank_app.models import Account, Passkey, PermissionSnapshot
from enterprise_platform.auth import AuthError
from enterprise_platform.authz import NormalizedGrant
from enterprise_platform.authz.snapshot_freshness import BackgroundRefresher, SnapshotFreshness, classify_snapshot
from enterprise_platform.request_scope import drop_request_memo, request_scope
from enterprise_platform.schemas import CurrentUser

logger = logging.getLogger(__name__)

_REFRESH_WORKERS = 2
_USABLE = frozenset(
    {
        SnapshotFreshness.FRESH,
        SnapshotFreshness.NEAR_EXPIRY,
        SnapshotFreshness.STALE_GRACE,
    }
)
_PRE_REFRESH = frozenset({SnapshotFreshness.NEAR_EXPIRY, SnapshotFreshness.STALE_GRACE})
# shutdown 后换新实例:框架 BackgroundRefresher 关机不可复用,TestClient lifespan 会反复启停。
_refresher = BackgroundRefresher(max_workers=_REFRESH_WORKERS, name="authz-refresh")


@dataclass(frozen=True)
class SnapshotState:
    grants: tuple[NormalizedGrant, ...]
    groups: list[str]
    freshness: SnapshotFreshness
    pull_key: str


def drop_auth_memo() -> None:
    """安全突变后丢掉请求内身份 memo,避免同一请求读到吊销前快照。"""

    drop_request_memo("current_user", "account")


def resolve_current_user() -> CurrentUser:
    memo = request_scope()
    cached = None if memo is None else memo.get("current_user")
    if cached is not None:
        return cached
    user, account = _build_current_user()
    if memo is not None:
        memo["current_user"] = user
        memo["account"] = account
    return user


def load_authenticated_account(db: Session) -> tuple[Account, bool]:
    from blank_app.adapter_account import _decode_session_token
    from blank_app.adapter_support import request_principal_account_id, request_token

    principal_id = request_principal_account_id.get()
    if principal_id:
        return _account_with_passkey(db, principal_id)
    token = request_token.get()
    if not token:
        raise AuthError(401, "未登录")
    return _account_from_claims(db, _decode_session_token(token))


def load_external_authz(account: Account) -> tuple[tuple[NormalizedGrant, ...], list[str]]:
    if not account.external_source or not account.external_user_id:
        return (), []
    with SessionLocal() as db:
        state = read_snapshot_state(db, account)
    return _finish_external_authz(account, state)


def read_snapshot_state(db: Session, account: Account) -> SnapshotState:
    now = datetime.now(UTC)
    app_key = cached_app_key(db)
    pull_key = f"{app_key}:{account.external_user_id}"
    row = _snapshot_row(db, account, app_key)
    if row is None:
        return SnapshotState((), [], SnapshotFreshness.EXPIRED, pull_key)
    return _state_from_row(db, row, now, pull_key, app_key)


def wait_background_refreshes(timeout: float = 5.0) -> None:
    _refresher.wait(timeout)


def shutdown_background_refresh() -> None:
    global _refresher
    _refresher.shutdown()
    _refresher = BackgroundRefresher(max_workers=_REFRESH_WORKERS, name="authz-refresh")


def schedule_background_refresh(account_id: uuid.UUID, pull_key: str) -> None:
    from blank_app.authz_snapshot import _in_failure_backoff

    if _in_failure_backoff(pull_key):
        return
    _refresher.schedule(pull_key, lambda: _run_background_refresh(account_id, pull_key))


def _build_current_user() -> tuple[CurrentUser, Account]:
    with SessionLocal() as db:
        account, _has_passkey = load_authenticated_account(db)
        grants, groups, pending = _authz_in_session(db, account)
        _expunge_if_present(db, account)
    if pending is not None:
        grants, groups = _finish_external_authz(account, pending)
    return _current_user_from(account, grants, groups), account


def _expunge_if_present(db: Session, account: Account) -> None:
    if object_session(account) is db:
        db.expunge(account)


def _authz_in_session(
    db: Session, account: Account
) -> tuple[tuple[NormalizedGrant, ...], list[str], SnapshotState | None]:
    from blank_app.adapter_account import _local_grants, _superadmin_grants
    from blank_app.adapter_support import is_local_superadmin

    if is_local_superadmin(account):
        return _superadmin_grants(), [], None
    if not account.external_source:
        return _local_grants(account), [], None
    state = read_snapshot_state(db, account)
    return state.grants, state.groups, state


def _finish_external_authz(account: Account, pending: SnapshotState) -> tuple[tuple[NormalizedGrant, ...], list[str]]:
    from blank_app.authz_snapshot import ensure_account_snapshot

    if _can_serve(pending):
        if pending.freshness in _PRE_REFRESH:
            schedule_background_refresh(account.id, pending.pull_key)
        return pending.grants, pending.groups
    ensure_account_snapshot(account.id, force=False)
    with SessionLocal() as db:
        again = read_snapshot_state(db, account)
    if _can_serve(again):
        return again.grants, again.groups
    return (), again.groups or pending.groups


def usable_snapshot_row(db: Session, account: Account) -> PermissionSnapshot | None:
    """与 /auth/me 同一套分类+目录下限:宽限行可读,失效/过宽限不可读。"""

    if not account.external_source or not account.external_user_id:
        return None
    now = datetime.now(UTC)
    app_key = cached_app_key(db)
    row = _snapshot_row(db, account, app_key)
    if row is None:
        return None
    state = _state_from_row(db, row, now, f"{app_key}:{account.external_user_id}", app_key)
    return row if _can_serve(state) else None


def _can_serve(state: SnapshotState) -> bool:
    return state.freshness in _USABLE


def _current_user_from(account: Account, grants: tuple[NormalizedGrant, ...], groups: list[str]) -> CurrentUser:
    from blank_app.adapter_support import ALL_PERMISSIONS, is_local_superadmin, security_capabilities

    codes = ALL_PERMISSIONS if is_local_superadmin(account) else {grant.code for grant in grants}
    return CurrentUser(
        id=str(account.id),
        account_id=str(account.id),
        is_local_superadmin=is_local_superadmin(account),
        name=account.username,
        email=account.email,
        avatar_url=account.avatar_url,
        ui_locale=account.ui_locale,
        must_change_password=account.must_change_password,
        has_local_password=bool(account.password_hash),
        permissions=sorted(codes),
        grants=list(grants),
        role_groups=groups,
        security_capabilities=security_capabilities(account),
    )


def _account_from_claims(db: Session, claims: dict[str, Any]) -> tuple[Account, bool]:
    account, has_passkey = _account_with_passkey(db, claims.get("sub"))
    issued_at = datetime.fromtimestamp(float(claims.get("session_started_at", claims["iat"])), tz=UTC)
    revoked_at = account.sessions_revoked_at
    if revoked_at and (revoked_at if revoked_at.tzinfo else revoked_at.replace(tzinfo=UTC)) >= issued_at:
        raise AuthError(401, "登录态已失效")
    return account, has_passkey


def _account_with_passkey(db: Session, account_id: object) -> tuple[Account, bool]:
    from blank_app.adapter_support import account_is_eligible

    account = db.get(Account, account_id)
    if account is None or not account_is_eligible(account):
        raise AuthError(401, "登录态无效")
    has_passkey = db.query(Passkey.id).filter(Passkey.account_id == account.id).first() is not None
    return account, has_passkey


def _snapshot_row(db: Session, account: Account, app_key: str) -> PermissionSnapshot | None:
    return (
        db.query(PermissionSnapshot)
        .filter(
            PermissionSnapshot.external_source == account.external_source,
            PermissionSnapshot.external_user_id == account.external_user_id,
            PermissionSnapshot.app_key == app_key,
        )
        .one_or_none()
    )


def _state_from_row(db: Session, row: PermissionSnapshot, now: datetime, pull_key: str, app_key: str) -> SnapshotState:
    from blank_app.authz_snapshot import _role_groups, _utc

    groups = _role_groups(row.groups)
    if row.catalog_version < cached_catalog_floor(db, app_key):
        return SnapshotState((), groups, SnapshotFreshness.EXPIRED, pull_key)
    freshness = classify_snapshot(_utc(row.fetched_at), _utc(row.expires_at), now)
    if freshness is SnapshotFreshness.EXPIRED:
        return SnapshotState((), [], freshness, pull_key)
    return SnapshotState(_normalized_grants(db, row), groups, freshness, pull_key)


def _normalized_grants(db: Session, row: PermissionSnapshot) -> tuple[NormalizedGrant, ...]:
    from enterprise_platform.authz import normalize_grants

    return tuple(
        NormalizedGrant(code=grant.code, scope=grant.scope) for grant in normalize_grants(row.grants, catalog_map(db))
    )


def _run_background_refresh(account_id: uuid.UUID, pull_key: str) -> None:
    from blank_app.authz_snapshot import _facade, _lock_for, _refresh_under_lock, _remember_pull_failure

    try:
        with _lock_for(pull_key):
            if _still_needs_refresh(account_id):
                _refresh_under_lock(account_id, force=True, pull_key=pull_key)
    except _facade().HTTPException:
        _remember_pull_failure(pull_key)
    except Exception:
        logger.exception("authz background refresh failed")
        _remember_pull_failure(pull_key)


def _still_needs_refresh(account_id: uuid.UUID) -> bool:
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if account is None or not account.external_source or not account.external_user_id:
            return False
        state = read_snapshot_state(db, account)
    return (not _can_serve(state)) or state.freshness in _PRE_REFRESH
