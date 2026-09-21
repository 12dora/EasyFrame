"""blank host NotificationSettingsPort: 平台策略、个人偏好与钉钉渠道。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert

from blank_app.database import SessionLocal
from blank_app.models import (
    PlatformAuditLog,
    PlatformNotificationPolicy,
    PlatformNotificationPreference,
    PlatformSetting,
)
from enterprise_platform.easyauth import (
    EasyAuthCredential,
    EasyAuthCredentialError,
    probe_notify_credential,
    resolve_bearer_token,
)
from enterprise_platform.notification_settings import (
    CHANNEL_DINGTALK,
    CHANNEL_IN_APP,
    DingtalkChannelSettings,
    DingtalkChannelSettingsUpdate,
    NotificationGroupManagedError,
    NotificationGroupPolicy,
    PolicyChange,
    SwitchChange,
    Switches,
)
from enterprise_platform.schemas import ConnectionTestResult
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret, encrypt_secret

_POLICY_AUDIT = "notification.policy.update"
_PREFERENCE_AUDIT = "notification.preferences.update"
_NOTIFY_KEY = "easyauth_notify"
_NOTIFY_AUDIT = "notification.channel.dingtalk.update"


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
            return _notify_row_configured() or _easyauth_notify_configured()
        return False

    def load_dingtalk_channel(self) -> DingtalkChannelSettings:
        stored, updated_at = _load_notify_row()
        return _dingtalk_from_stored(stored, updated_at, configured=self.channel_available(CHANNEL_DINGTALK))

    def save_dingtalk_channel(
        self, payload: DingtalkChannelSettingsUpdate, *, actor_id: str
    ) -> DingtalkChannelSettings:
        stored, _updated_at = _load_notify_row()
        _facade()._save_setting(
            _NOTIFY_KEY, _next_notify_settings(stored, payload), actor_id=actor_id, action=_NOTIFY_AUDIT
        )
        return self.load_dingtalk_channel()

    def test_dingtalk_channel(self) -> ConnectionTestResult:
        stored, _updated_at = _load_notify_row()
        base_url, app_key, credential = _effective_notify_connection(stored, include_legacy=True)
        return probe_notify_credential(base_url, app_key, credential)


notification_settings_adapter = BlankNotificationSettingsAdapter()


def probe_dingtalk_notify() -> ConnectionTestResult:
    """系统服务「通知服务（钉钉）」探针:与渠道页「测试连接」同一条不发消息的校验。"""
    return notification_settings_adapter.test_dingtalk_channel()


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
    return bool(_easyauth_static_token())


def _notify_row_configured() -> bool:
    stored, _updated_at = _load_notify_row()
    base_url, app_key, credential = _effective_notify_connection(stored, include_legacy=False)
    return bool(base_url and app_key and credential)


def _load_notify_row() -> tuple[dict, datetime | None]:
    with SessionLocal() as db:
        row = db.get(PlatformSetting, _NOTIFY_KEY)
        if row is None:
            return {}, None
        return dict(row.value or {}), row.updated_at


def _dingtalk_from_stored(stored: dict, updated_at: datetime | None, *, configured: bool) -> DingtalkChannelSettings:
    stored_base, stored_key, credential = _stored_notify_parts(stored)
    inherited_base, inherited_key = _easyauth_identity()
    has_credential = bool(credential)
    return DingtalkChannelSettings(
        base_url=stored_base or inherited_base,
        app_key=stored_key or inherited_key,
        base_url_inherited=not stored_base,
        app_key_inherited=not stored_key,
        has_credential=has_credential,
        credential_source="settings" if has_credential else "none",
        configured=configured,
        updated_at=updated_at,
    )


def _next_notify_settings(stored: dict, payload: DingtalkChannelSettingsUpdate) -> dict:
    base_url = _kept_or_new(payload.base_url, stored.get("base_url"))
    app_key = _kept_or_new(payload.app_key, stored.get("app_key"))
    return {
        "base_url": base_url.rstrip("/") if base_url else "",
        "app_key": app_key,
        "credential": _next_stored_credential(payload.plain_credential(), stored.get("credential")),
    }


def _next_stored_credential(incoming: str | None, stored: object) -> str:
    # keep: 原样拷贝已存密文。解密失败只应让读/探活视为无可用凭据,不能在保存时把库里的值写成空。
    if incoming is None:
        return str(stored or "")
    if not incoming:
        return ""
    return encrypt_secret(incoming)


def _kept_or_new(incoming: str | None, stored: object) -> str:
    if incoming is None:
        return str(stored or "").strip()
    return incoming.strip()


def _effective_notify_connection(stored: dict, *, include_legacy: bool) -> tuple[str, str, str]:
    stored_base, stored_key, credential = _stored_notify_parts(stored)
    inherited_base, inherited_key = _easyauth_identity()
    if include_legacy and not credential:
        credential = _easyauth_static_token()
    return stored_base or inherited_base, stored_key or inherited_key, credential


def _stored_notify_parts(stored: dict) -> tuple[str, str, str]:
    return (
        str(stored.get("base_url") or "").strip().rstrip("/"),
        str(stored.get("app_key") or "").strip(),
        _decrypt_notify_credential(stored.get("credential")),
    )


def _easyauth_identity() -> tuple[str, str]:
    data = _facade()._get_setting("easyauth")
    return str(data.get("base_url") or "").strip().rstrip("/"), str(data.get("app_key") or "").strip()


def _easyauth_static_token() -> str:
    data = _facade()._get_setting("easyauth")
    if (data.get("auth_mode") or "static_app_token") != "static_app_token":
        return ""
    try:
        return resolve_bearer_token(
            EasyAuthCredential(
                base_url=str(data.get("base_url") or ""),
                app_key=str(data.get("app_key") or ""),
                auth_mode="static_app_token",
                credential=decrypt_secret(str(data.get("credential") or "")),
            )
        )
    except (SecretConfigurationError, EasyAuthCredentialError):
        return ""


def _decrypt_notify_credential(raw: object) -> str:
    # 读/探活 fail-closed: 密钥缺失或密文损坏视为无可用凭据,但不改存储。
    try:
        return decrypt_secret(str(raw or ""))
    except SecretConfigurationError:
        return ""
