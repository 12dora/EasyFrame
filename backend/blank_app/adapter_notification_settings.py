"""blank host NotificationSettingsPort: 平台策略与个人偏好。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert

from blank_app.database import SessionLocal
from blank_app.models import PlatformAuditLog, PlatformNotificationPolicy, PlatformNotificationPreference
from enterprise_platform.easyauth import EasyAuthCredential, EasyAuthCredentialError, resolve_bearer_token
from enterprise_platform.notification_settings import (
    CHANNEL_DINGTALK,
    CHANNEL_IN_APP,
    NotificationGroupManagedError,
    NotificationGroupPolicy,
    PolicyChange,
    SwitchChange,
    Switches,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret

_POLICY_AUDIT = "notification.policy.update"
_PREFERENCE_AUDIT = "notification.preferences.update"


def _facade():
    from blank_app import adapters

    return adapters


class BlankNotificationSettingsAdapter:
    def load_policies(self) -> dict[str, NotificationGroupPolicy]:
        with SessionLocal() as db:
            rows = db.query(PlatformNotificationPolicy).all()
            return {row.group_key: _to_policy(row) for row in rows}

    def save_policy(self, group_key: str, change: PolicyChange, *, actor_id: str) -> NotificationGroupPolicy:
        with SessionLocal() as db:
            row = _lock_policy(db, group_key)
            before = _policy_payload(row)
            if change.managed is not None:
                row.managed = change.managed
            if change.switch is not None:
                row.switches = _apply_switch(_copy_switches(row.switches), change.switch)
            row.updated_by = actor_id
            _write_audit(db, actor_id, _POLICY_AUDIT, before, _policy_payload(row))
            db.commit()
            return _to_policy(row)

    def load_preferences(self, account_ids: Sequence[str], group_key: str) -> dict[str, Switches]:
        if not account_ids:
            return {}
        ids = [uuid.UUID(str(item)) for item in account_ids]
        with SessionLocal() as db:
            rows = (
                db.query(PlatformNotificationPreference)
                .filter(
                    PlatformNotificationPreference.account_id.in_(ids),
                    PlatformNotificationPreference.group_key == group_key,
                )
                .all()
            )
            return {str(row.account_id): _copy_switches(row.switches) for row in rows}

    def save_preference(self, account_id: str, group_key: str, change: SwitchChange) -> Switches:
        with SessionLocal() as db:
            if _lock_policy(db, group_key).managed:
                raise NotificationGroupManagedError(group_key)
            row = _lock_preference(db, account_id, group_key)
            before = _preference_payload(row)
            row.switches = _apply_switch(_copy_switches(row.switches), change)
            _write_audit(db, account_id, _PREFERENCE_AUDIT, before, _preference_payload(row))
            db.commit()
            return _copy_switches(row.switches)

    def channel_available(self, channel: str) -> bool:
        if channel == CHANNEL_IN_APP:
            return True
        if channel == CHANNEL_DINGTALK:
            return _easyauth_notify_configured()
        return False


notification_settings_adapter = BlankNotificationSettingsAdapter()


def _lock_policy(db, group_key: str) -> PlatformNotificationPolicy:
    db.execute(
        pg_insert(PlatformNotificationPolicy)
        .values(group_key=group_key, managed=True, switches={})
        .on_conflict_do_nothing(index_elements=["group_key"])
    )
    return db.query(PlatformNotificationPolicy).filter_by(group_key=group_key).with_for_update().one()


def _lock_preference(db, account_id: str, group_key: str) -> PlatformNotificationPreference:
    account_uuid = uuid.UUID(str(account_id))
    db.execute(
        pg_insert(PlatformNotificationPreference)
        .values(account_id=account_uuid, group_key=group_key, switches={})
        .on_conflict_do_nothing(index_elements=["account_id", "group_key"])
    )
    return (
        db.query(PlatformNotificationPreference)
        .filter_by(account_id=account_uuid, group_key=group_key)
        .with_for_update()
        .one()
    )


def _copy_switches(raw: object) -> Switches:
    if not isinstance(raw, dict):
        return {}
    return {str(scene): dict(channels) for scene, channels in raw.items() if isinstance(channels, dict)}


def _apply_switch(switches: Switches, change: SwitchChange) -> Switches:
    updated = {scene: dict(channels) for scene, channels in switches.items()}
    scene_map = dict(updated.get(change.scene, {}))
    scene_map[change.channel] = change.enabled
    updated[change.scene] = scene_map
    return updated


def _to_policy(row: PlatformNotificationPolicy) -> NotificationGroupPolicy:
    return NotificationGroupPolicy(managed=row.managed, switches=_copy_switches(row.switches))


def _policy_payload(row: PlatformNotificationPolicy) -> dict:
    return {"group_key": row.group_key, "managed": row.managed, "switches": _copy_switches(row.switches)}


def _preference_payload(row: PlatformNotificationPreference) -> dict:
    return {"account_id": str(row.account_id), "group_key": row.group_key, "switches": _copy_switches(row.switches)}


def _write_audit(db, actor_id: str, action: str, before: dict, after: dict) -> None:
    db.add(
        PlatformAuditLog(
            actor_id=str(actor_id).strip()[:100],
            action=action,
            before_data=before,
            after_data=after,
        )
    )


def _easyauth_notify_configured() -> bool:
    data = _facade()._get_setting("easyauth")
    if (data.get("auth_mode") or "static_app_token") != "static_app_token":
        return False
    try:
        resolve_bearer_token(
            EasyAuthCredential(
                base_url=str(data.get("base_url") or ""),
                app_key=str(data.get("app_key") or ""),
                auth_mode="static_app_token",
                credential=decrypt_secret(str(data.get("credential") or "")),
            )
        )
    except (SecretConfigurationError, EasyAuthCredentialError):
        return False
    return True
