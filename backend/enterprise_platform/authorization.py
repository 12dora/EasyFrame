"""可复用 EasyAuth 授权运维 router factory。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from enterprise_platform.auth import AuthError
from enterprise_platform.ports import AuthorizationOperationsPort
from enterprise_platform.response_redaction import (
    has_permission,
    present_authorization_catalog,
    present_authorization_settings,
    present_authorization_snapshots,
    present_authorization_status,
    present_my_grants,
)
from enterprise_platform.schemas import (
    AuthorizationCatalogItem,
    AuthorizationCatalogSummary,
    AuthorizationConnectionResult,
    AuthorizationSettings,
    AuthorizationSettingsSummary,
    AuthorizationSettingsUpdate,
    AuthorizationSnapshot,
    AuthorizationSnapshotSummary,
    AuthorizationStatus,
    AuthorizationStatusSummary,
    CurrentUser,
    DescriptorKeyCreateRequest,
    DescriptorKeyCreateResponse,
    DescriptorKeyResponse,
    DescriptorKeyUpdateRequest,
    MyGrantResponse,
    MyGrantSummary,
)

AUTHZ_VIEW = "authz.integration.view"
AUTHZ_MANAGE = "authz.integration.manage"
_SNAPSHOT_SORTS = {"fetched_at_desc", "fetched_at_asc", "expires_at_desc", "expires_at_asc"}


def create_authorization_operations_router(
    operations: AuthorizationOperationsPort,
    *,
    current_user_dependency: Callable[..., CurrentUser],
    permission_dependency_factory: Callable[[str], Callable[..., Any]],
) -> APIRouter:
    """创建共享授权运维 API；宿主只注入持久化、上游与审计 adapter。"""

    router = APIRouter(prefix="/authz-integration", tags=["authz-integration"])

    def port_call(callback: Callable[[], Any]) -> Any:
        try:
            return callback()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def actor(user: CurrentUser = Depends(current_user_dependency)) -> CurrentUser:
        if user.must_change_password:
            raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
        return user

    @router.get("/status", response_model=AuthorizationStatusSummary | AuthorizationStatus)
    def status(
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_VIEW)),
    ) -> AuthorizationStatusSummary | AuthorizationStatus:
        value = port_call(operations.status)
        return present_authorization_status(
            value,
            can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
        )

    @router.post("/connection-test", response_model=AuthorizationConnectionResult)
    def connection_test(
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> AuthorizationConnectionResult:
        return port_call(lambda: operations.connection_test(actor_id=user.id))

    @router.get("/settings", response_model=AuthorizationSettingsSummary | AuthorizationSettings)
    def get_settings(
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_VIEW)),
    ) -> AuthorizationSettingsSummary | AuthorizationSettings:
        value = port_call(operations.get_settings)
        return present_authorization_settings(
            value,
            can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
        )

    @router.put("/settings", response_model=AuthorizationSettings)
    def save_settings(
        body: AuthorizationSettingsUpdate,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> AuthorizationSettings:
        return port_call(lambda: operations.save_settings(body, actor_id=user.id))

    @router.get(
        "/permission-catalog",
        response_model=list[AuthorizationCatalogSummary] | list[AuthorizationCatalogItem],
    )
    def catalog(
        search: str | None = Query(None, max_length=160),
        active: bool | None = Query(None),
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_VIEW)),
    ) -> list[AuthorizationCatalogSummary] | list[AuthorizationCatalogItem]:
        values = port_call(lambda: operations.list_catalog(search=search, active=active))
        return present_authorization_catalog(
            values,
            can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
        )

    @router.get(
        "/snapshots",
        response_model=list[AuthorizationSnapshotSummary] | list[AuthorizationSnapshot],
    )
    def snapshots(
        response: Response,
        search: str | None = Query(None, max_length=160),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        sort: str = Query("fetched_at_desc"),
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_VIEW)),
    ) -> list[AuthorizationSnapshotSummary] | list[AuthorizationSnapshot]:
        if sort not in _SNAPSHOT_SORTS:
            raise HTTPException(400, "unsupported snapshot sort")
        rows, total = port_call(lambda: operations.list_snapshots(search=search, offset=offset, limit=limit, sort=sort))
        response.headers["X-Total-Count"] = str(total)
        return present_authorization_snapshots(
            rows,
            can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
        )

    @router.post("/snapshots/{user_id}/refresh", response_model=AuthorizationSnapshot)
    def refresh_snapshot(
        user_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> AuthorizationSnapshot:
        return port_call(lambda: operations.refresh_snapshot(user_id, actor_id=user.id))

    @router.get("/manifest")
    def manifest(
        schema_version: int | None = Query(None, ge=1),
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> dict[str, Any]:
        return port_call(lambda: operations.manifest(schema_version=schema_version, actor_id=user.id))

    @router.get("/my-grants", response_model=list[MyGrantSummary] | list[MyGrantResponse])
    def my_grants(
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_VIEW)),
    ) -> list[MyGrantSummary] | list[MyGrantResponse]:
        values = port_call(lambda: operations.my_grants(actor_id=user.id))
        return present_my_grants(
            values,
            can_manage=has_permission(user, _permission, AUTHZ_MANAGE),
        )

    @router.get("/descriptor-keys", response_model=list[DescriptorKeyResponse])
    def descriptor_keys(
        _user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> list[DescriptorKeyResponse]:
        return port_call(operations.list_descriptor_keys)

    @router.post("/descriptor-keys", response_model=DescriptorKeyCreateResponse, status_code=201)
    def create_descriptor_key(
        body: DescriptorKeyCreateRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> DescriptorKeyCreateResponse:
        return port_call(lambda: operations.create_descriptor_key(body, actor_id=user.id))

    @router.patch("/descriptor-keys/{key_id}", response_model=DescriptorKeyResponse)
    def update_descriptor_key(
        key_id: uuid.UUID,
        body: DescriptorKeyUpdateRequest,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> DescriptorKeyResponse:
        return port_call(lambda: operations.update_descriptor_key(key_id, body, actor_id=user.id))

    @router.delete("/descriptor-keys/{key_id}", status_code=204)
    def delete_descriptor_key(
        key_id: uuid.UUID,
        user: CurrentUser = Depends(actor),
        _permission: Any = Depends(permission_dependency_factory(AUTHZ_MANAGE)),
    ) -> Response:
        port_call(lambda: operations.delete_descriptor_key(key_id, actor_id=user.id))
        return Response(status_code=204)

    return router
