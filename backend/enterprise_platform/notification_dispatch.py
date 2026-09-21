"""通知发送判定:读端口、算生效开关,不发送。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from enterprise_platform.notification_settings import (
    CHANNEL_DINGTALK,
    CHANNEL_IN_APP,
    NotificationCatalog,
    NotificationGroupPolicy,
    NotificationScene,
    NotificationSettingsPort,
    Switches,
    effective_enabled,
)


class UnknownNotificationSceneError(ValueError):
    """未在目录中声明的 scene_key,属于编程错误。"""

    def __init__(self, scene_key: str) -> None:
        super().__init__(scene_key)
        self.scene_key = scene_key


@dataclass(frozen=True)
class NotificationRecipient:
    ref: str
    account_id: str | None


@dataclass(frozen=True)
class DeliveryPlan:
    scene_key: str
    in_app: tuple[NotificationRecipient, ...]
    dingtalk: tuple[NotificationRecipient, ...]


def plan_delivery(
    catalog: NotificationCatalog,
    settings: NotificationSettingsPort,
    scene_key: str,
    recipients: Sequence[NotificationRecipient],
) -> DeliveryPlan:
    located = catalog.scene(scene_key)
    if located is None:
        raise UnknownNotificationSceneError(scene_key)
    group, scene = located
    policy = settings.load_policies().get(group.key, NotificationGroupPolicy())
    unique = _dedupe_recipients(recipients)
    prefs = _load_plan_preferences(settings, policy, group.key, unique)
    return DeliveryPlan(
        scene_key=scene_key,
        in_app=_planned(scene, CHANNEL_IN_APP, policy, prefs, unique, require_account=True),
        dingtalk=_planned(scene, CHANNEL_DINGTALK, policy, prefs, unique, require_account=False),
    )


def _load_plan_preferences(
    settings: NotificationSettingsPort,
    policy: NotificationGroupPolicy,
    group_key: str,
    recipients: Sequence[NotificationRecipient],
) -> dict[str, Switches]:
    if policy.managed:
        return {}
    account_ids = _unique_account_ids(recipients)
    if not account_ids:
        return {}
    return settings.load_preferences(account_ids, group_key)


def _unique_account_ids(recipients: Sequence[NotificationRecipient]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        account_id = recipient.account_id
        if account_id and account_id not in seen:
            seen.add(account_id)
            ids.append(account_id)
    return ids


def _dedupe_recipients(recipients: Sequence[NotificationRecipient]) -> tuple[NotificationRecipient, ...]:
    unique: list[NotificationRecipient] = []
    seen_refs: set[str] = set()
    seen_accounts: set[str] = set()
    for recipient in recipients:
        account_id = recipient.account_id
        if recipient.ref in seen_refs or (account_id is not None and account_id in seen_accounts):
            continue
        seen_refs.add(recipient.ref)
        if account_id is not None:
            seen_accounts.add(account_id)
        unique.append(recipient)
    return tuple(unique)


def _planned(
    scene: NotificationScene,
    channel: str,
    policy: NotificationGroupPolicy,
    prefs: dict[str, Switches],
    recipients: Sequence[NotificationRecipient],
    *,
    require_account: bool,
) -> tuple[NotificationRecipient, ...]:
    if channel not in scene.channels:
        return ()
    return tuple(
        recipient
        for recipient in recipients
        if _recipient_enabled(scene, channel, policy, prefs, recipient, require_account=require_account)
    )


def _recipient_enabled(
    scene: NotificationScene,
    channel: str,
    policy: NotificationGroupPolicy,
    prefs: dict[str, Switches],
    recipient: NotificationRecipient,
    *,
    require_account: bool,
) -> bool:
    if require_account and recipient.account_id is None:
        return False
    preference = prefs.get(recipient.account_id) if recipient.account_id else None
    return effective_enabled(
        scene,
        channel,
        managed=policy.managed,
        has_account=recipient.account_id is not None,
        policy=policy.switches,
        preference=preference,
    )
