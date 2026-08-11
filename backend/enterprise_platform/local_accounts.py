"""本地账户管理共享 router factory。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field

from enterprise_platform.auth import AuthError
from enterprise_platform.authz import RiskLevel
from enterprise_platform.schemas import CurrentUser, PasswordValue, PlatformModel, StrictPlatformModel

LOCAL_ACCOUNTS_VIEW = "accounts.local.view"
LOCAL_ACCOUNTS_MANAGE = "accounts.local.manage"
BASELINE_SELF_SERVICE = {
    "auth.totp.create",
    "auth.totp.advance",
    "auth.passkey.view",
    "auth.passkey.create",
    "notification.center.view",
}


class LocalAccountSummary(PlatformModel):
    id: uuid.UUID
    username: str
    email: str | None = None
    active: bool
    is_admin: bool
    totp_enabled: bool
    passkey_count: int
    must_change_password: bool
    permission_count: int
    created_at: datetime


class LocalAccountDetail(LocalAccountSummary):
    ui_locale: str
    permissions: list[str] = Field(default_factory=list)
    baseline_permissions: list[str] = Field(default_factory=list)


class LocalAccountListMeta(PlatformModel):
    total: int


class LocalAccountListResponse(PlatformModel):
    data: list[LocalAccountSummary]
    meta: LocalAccountListMeta


class LocalAccountPermissionCatalogItem(PlatformModel):
    code: str
    domain: str
    resource: str
    risk_level: RiskLevel
    active: bool


class LocalAccountPermissionCatalogResponse(PlatformModel):
    data: list[LocalAccountPermissionCatalogItem]


class CreateLocalAccountRequest(StrictPlatformModel):
    username: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    password: PasswordValue
    must_change_password: bool = True
    is_admin: bool = False
    permissions: list[str] = Field(default_factory=list)


class UpdateLocalAccountRequest(StrictPlatformModel):
    email: str | None = Field(default=None, max_length=200)
    active: bool | None = None
    is_admin: bool | None = None
    ui_locale: str | None = Field(default=None, max_length=10)


class ResetLocalAccountPasswordRequest(StrictPlatformModel):
    password: PasswordValue
    must_change_password: bool = True


class SetLocalAccountPermissionsRequest(StrictPlatformModel):
    permissions: list[str] = Field(default_factory=list)


LocalAccountCreateRequest = CreateLocalAccountRequest
LocalAccountUpdateRequest = UpdateLocalAccountRequest
LocalAccountPasswordRequest = ResetLocalAccountPasswordRequest
LocalAccountPermissionsRequest = SetLocalAccountPermissionsRequest


class LocalAccountAdminPort(Protocol):
    def list(self, *, search: str | None) -> tuple[list[LocalAccountSummary], int]: ...
    def permission_catalog(self) -> list[LocalAccountPermissionCatalogItem]: ...
    def create(self, payload: CreateLocalAccountRequest, *, actor_id: str) -> LocalAccountDetail: ...
    def get(self, account_id: uuid.UUID) -> LocalAccountDetail: ...
    def update(
        self, account_id: uuid.UUID, payload: UpdateLocalAccountRequest, *, actor_id: str
    ) -> LocalAccountDetail: ...
    def delete(self, account_id: uuid.UUID, *, actor_id: str) -> None: ...
    def reset_password(
        self, account_id: uuid.UUID, payload: ResetLocalAccountPasswordRequest, *, actor_id: str
    ) -> LocalAccountDetail: ...
    def set_permissions(
        self, account_id: uuid.UUID, payload: SetLocalAccountPermissionsRequest, *, actor_id: str
    ) -> LocalAccountDetail: ...
    def disable_totp(self, account_id: uuid.UUID, *, actor_id: str) -> LocalAccountDetail: ...


def create_local_accounts_router(
    operations: LocalAccountAdminPort,
    *,
    current_user_dependency: Callable[..., CurrentUser],
    permission_dependency_factory: Callable[[str], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(prefix="/local-accounts", tags=["local-accounts"])

    def port_call(callback):
        try:
            return callback()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def actor(user: CurrentUser = Depends(current_user_dependency)) -> CurrentUser:
        if user.must_change_password:
            raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
        return user

    @router.get("", response_model=LocalAccountListResponse)
    def list_local_accounts(
        search: str | None = Query(None, max_length=160),
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountListResponse:
        rows, total = port_call(lambda: operations.list(search=search))
        return LocalAccountListResponse(data=rows, meta=LocalAccountListMeta(total=total))

    @router.post("", response_model=LocalAccountDetail, status_code=201)
    def create_local_account(
        body: CreateLocalAccountRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.create(body, actor_id=user.id))

    @router.get("/permission-catalog", response_model=LocalAccountPermissionCatalogResponse)
    def get_local_account_permission_catalog(
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountPermissionCatalogResponse:
        return LocalAccountPermissionCatalogResponse(data=port_call(operations.permission_catalog))

    @router.get("/{account_id}", response_model=LocalAccountDetail)
    def get_local_account(
        account_id: uuid.UUID,
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_VIEW)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.get(account_id))

    @router.patch("/{account_id}", response_model=LocalAccountDetail)
    def update_local_account(
        account_id: uuid.UUID,
        body: UpdateLocalAccountRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.update(account_id, body, actor_id=user.id))

    @router.delete("/{account_id}", status_code=204)
    def delete_local_account(
        account_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> Response:
        port_call(lambda: operations.delete(account_id, actor_id=user.id))
        return Response(status_code=204)

    @router.post("/{account_id}/password", response_model=LocalAccountDetail)
    def reset_local_account_password(
        account_id: uuid.UUID,
        body: ResetLocalAccountPasswordRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.reset_password(account_id, body, actor_id=user.id))

    @router.put("/{account_id}/permissions", response_model=LocalAccountDetail)
    def set_local_account_permissions(
        account_id: uuid.UUID,
        body: SetLocalAccountPermissionsRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.set_permissions(account_id, body, actor_id=user.id))

    @router.delete("/{account_id}/totp", response_model=LocalAccountDetail)
    def disable_local_account_totp(
        account_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(LOCAL_ACCOUNTS_MANAGE)),
    ) -> LocalAccountDetail:
        return port_call(lambda: operations.disable_totp(account_id, actor_id=user.id))

    return router


create_local_account_admin_router = create_local_accounts_router
