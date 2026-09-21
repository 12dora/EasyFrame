"""通知设置 HTTP 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from enterprise_platform.assembly.contracts import NOTIFICATION_SETTINGS_MANAGE
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.notification_settings import (
    NotificationCatalog,
    NotificationGroup,
    NotificationGroupPolicy,
    NotificationGroupView,
    NotificationPolicyPatch,
    NotificationPolicyView,
    NotificationPreferencePatch,
    NotificationScene,
    NotificationSettingsPort,
    NotificationSettingsView,
    PolicyChange,
    SwitchChange,
    UnknownNotificationTargetError,
    build_group_view,
    build_my_settings,
    build_policy_settings,
    validate_change,
)
from enterprise_platform.schemas import CurrentUser

_MANAGED = {"code": "notification_group_managed"}
_NOT_FOUND = "通知设置不存在"
_FORBIDDEN = "缺少权限"


def register_notification_settings_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_get_mine(router, ctx)
    _register_patch_preferences(router, ctx)
    _register_get_policy(router, ctx)
    _register_patch_policy(router, ctx)


def _register_get_mine(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get(
        "/notification-settings",
        response_model=NotificationSettingsView,
        tags=["notification-settings"],
    )
    def get_mine(user: CurrentUser = Depends(ctx.current_user)) -> NotificationSettingsView:
        return _get_mine(ctx, user)


def _register_patch_preferences(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.patch(
        "/notification-settings/preferences",
        response_model=NotificationGroupView,
        tags=["notification-settings"],
    )
    def patch_preferences(
        body: NotificationPreferencePatch,
        user: CurrentUser = Depends(ctx.current_user),
    ) -> NotificationGroupView:
        return _patch_preferences(ctx, body, user)


def _register_get_policy(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get(
        "/notification-settings/policy",
        response_model=NotificationPolicyView,
        tags=["notification-settings"],
    )
    def get_policy(
        _user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(NOTIFICATION_SETTINGS_MANAGE)),
    ) -> NotificationPolicyView:
        return _get_policy(ctx)


def _register_patch_policy(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.patch(
        "/notification-settings/policy",
        response_model=NotificationGroupView,
        tags=["notification-settings"],
    )
    def patch_policy(
        body: NotificationPolicyPatch,
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(NOTIFICATION_SETTINGS_MANAGE)),
    ) -> NotificationGroupView:
        return _patch_policy(ctx, body, user)


def _get_mine(ctx: AssemblyDependencies, user: CurrentUser) -> NotificationSettingsView:
    catalog, port = _bound_ports(ctx)
    return build_my_settings(
        catalog,
        port,
        account_id=user.id,
        permissions=user.permissions,
        can_manage=NOTIFICATION_SETTINGS_MANAGE in user.permissions,
    )


def _get_policy(ctx: AssemblyDependencies) -> NotificationPolicyView:
    catalog, port = _bound_ports(ctx)
    return build_policy_settings(catalog, port)


def _patch_preferences(
    ctx: AssemblyDependencies,
    body: NotificationPreferencePatch,
    user: CurrentUser,
) -> NotificationGroupView:
    catalog, port = _bound_ports(ctx)
    group, _scene = _require_target(catalog, body.group, body.scene, body.channel)
    _require_gate(user, group)
    policy = _group_policy(port, group.key)
    if policy.managed:
        raise HTTPException(409, _MANAGED)
    change = SwitchChange(scene=body.scene, channel=body.channel, enabled=body.enabled)
    switches = ctx.port_call(lambda: port.save_preference(user.id, group.key, change))
    return build_group_view(group, policy, switches, mode="my")


def _patch_policy(ctx: AssemblyDependencies, body: NotificationPolicyPatch, user: CurrentUser) -> NotificationGroupView:
    catalog, port = _bound_ports(ctx)
    group = _require_group(catalog, body.group)
    change = _validated_policy_change(catalog, group, body)
    policy = ctx.port_call(lambda: port.save_policy(group.key, change, actor_id=user.id))
    return build_group_view(group, policy, None, mode="policy")


def _validated_policy_change(
    catalog: NotificationCatalog, group: NotificationGroup, body: NotificationPolicyPatch
) -> PolicyChange:
    change = body.to_policy_change()
    if change.switch is not None:
        _require_target(catalog, group.key, change.switch.scene, change.switch.channel)
    return change


def _bound_ports(ctx: AssemblyDependencies) -> tuple[NotificationCatalog, NotificationSettingsPort]:
    catalog = ctx.ports.notification_catalog
    settings = ctx.ports.notification_settings
    if catalog is None or settings is None:
        raise HTTPException(404, _NOT_FOUND)
    return catalog, settings


def _require_group(catalog: NotificationCatalog, group_key: str) -> NotificationGroup:
    group = catalog.group(group_key)
    if group is None:
        raise HTTPException(404, _NOT_FOUND)
    return group


def _require_target(
    catalog: NotificationCatalog, group_key: str, scene_key: str, channel: str
) -> tuple[NotificationGroup, NotificationScene]:
    try:
        return validate_change(catalog, group_key, scene_key, channel)
    except UnknownNotificationTargetError as exc:
        raise HTTPException(404, _NOT_FOUND) from exc


def _require_gate(user: CurrentUser, group: NotificationGroup) -> None:
    if group.gate_permission not in user.permissions:
        raise HTTPException(403, _FORBIDDEN)


def _group_policy(port: NotificationSettingsPort, group_key: str) -> NotificationGroupPolicy:
    return port.load_policies().get(group_key, NotificationGroupPolicy())
