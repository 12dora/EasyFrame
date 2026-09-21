"""blank host 钉钉渠道:easyauth_notify 存取、继承与上游探活。"""

from __future__ import annotations

import pytest

from blank_app.adapter_notification_settings import notification_settings_adapter
from blank_app.adapter_platform import BlankUpstreamHealthAdapter
from blank_app.adapter_support import _get_setting, _save_setting
from blank_app.database import SessionLocal
from blank_app.models import PlatformAuditLog, PlatformSetting
from enterprise_platform.notification_settings import CHANNEL_DINGTALK, DingtalkChannelSettingsUpdate
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret, encrypt_secret

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

_NOTIFY_KEY = "easyauth_notify"
_NOTIFY_AUDIT = "notification.channel.dingtalk.update"
_ACTOR = "test-actor"
_BASE = "https://easyauth.example.test"
_APP = "enterprise-blank"
_SECRET = "eat_notify_blank_secret"
_LEGACY = "eat_easyauth_static_token"


@pytest.fixture(autouse=True)
def _restore_identity_rows():
    previous = _get_setting("easyauth")
    yield
    _save_setting("easyauth", previous, actor_id=_ACTOR, action="authz.settings.update")
    with SessionLocal() as db:
        row = db.get(PlatformSetting, _NOTIFY_KEY)
        if row is not None:
            db.delete(row)
        db.query(PlatformAuditLog).filter(PlatformAuditLog.action == _NOTIFY_AUDIT).delete(synchronize_session=False)
        db.commit()


def test_save_load_encrypts_credential_and_audits() -> None:
    saved = notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=_BASE, app_key=_APP, credential=_SECRET),
        actor_id=_ACTOR,
    )
    assert saved.base_url == _BASE
    assert saved.app_key == _APP
    assert saved.has_credential is True
    assert saved.credential_source == "settings"
    assert saved.configured is True
    assert saved.updated_at is not None
    loaded = notification_settings_adapter.load_dingtalk_channel()
    assert loaded.has_credential is True
    assert loaded.base_url == _BASE
    stored = _get_setting(_NOTIFY_KEY)
    assert stored["credential"].startswith("enc:v1:")
    assert decrypt_secret(stored["credential"]) == _SECRET
    assert _SECRET not in stored["credential"]
    with SessionLocal() as db:
        audit = (
            db.query(PlatformAuditLog)
            .filter(PlatformAuditLog.action == _NOTIFY_AUDIT)
            .order_by(PlatformAuditLog.created_at.desc())
            .one()
        )
        assert audit.after_data["credential"] == "[configured]"
        assert _SECRET not in str(audit.after_data)


def test_inherit_base_url_and_app_key_from_easyauth() -> None:
    _save_easyauth(base_url=_BASE, app_key=_APP, credential="")
    saved = notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(credential=_SECRET), actor_id=_ACTOR
    )
    assert saved.base_url == _BASE
    assert saved.app_key == _APP
    assert saved.base_url_inherited is True
    assert saved.app_key_inherited is True
    assert saved.has_credential is True
    assert saved.configured is True


def test_save_none_keeps_stored_values() -> None:
    notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=_BASE, app_key=_APP, credential=_SECRET),
        actor_id=_ACTOR,
    )
    kept = notification_settings_adapter.save_dingtalk_channel(DingtalkChannelSettingsUpdate(), actor_id=_ACTOR)
    assert kept.base_url == _BASE
    assert kept.app_key == _APP
    assert kept.has_credential is True
    assert decrypt_secret(_get_setting(_NOTIFY_KEY)["credential"]) == _SECRET


def test_keep_save_preserves_ciphertext_when_decrypt_fails(monkeypatch) -> None:
    notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=_BASE, app_key=_APP, credential=_SECRET),
        actor_id=_ACTOR,
    )
    ciphertext = _get_setting(_NOTIFY_KEY)["credential"]
    assert ciphertext.startswith("enc:v1:")

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise SecretConfigurationError("envelope key missing or rotated")

    monkeypatch.setattr("blank_app.adapter_notification_settings.decrypt_secret", _boom)
    notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=f"{_BASE}/v2"),
        actor_id=_ACTOR,
    )
    assert _get_setting(_NOTIFY_KEY)["credential"] == ciphertext


def test_clear_stored_values_falls_back_to_inherited() -> None:
    _save_easyauth(base_url=_BASE, app_key=_APP, credential="")
    notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=f"{_BASE}/notify", app_key="notify-app", credential=_SECRET),
        actor_id=_ACTOR,
    )
    cleared = notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url="", app_key="", credential=""),
        actor_id=_ACTOR,
    )
    assert cleared.base_url == _BASE
    assert cleared.app_key == _APP
    assert cleared.base_url_inherited is True
    assert cleared.app_key_inherited is True
    assert cleared.has_credential is False
    assert cleared.credential_source == "none"
    assert _get_setting(_NOTIFY_KEY)["credential"] == ""


def test_channel_available_notify_row_or_legacy_token() -> None:
    _save_easyauth(base_url="", app_key="", credential="")
    assert notification_settings_adapter.channel_available(CHANNEL_DINGTALK) is False
    _save_easyauth(base_url=_BASE, app_key=_APP, credential=_LEGACY)
    assert notification_settings_adapter.channel_available(CHANNEL_DINGTALK) is True
    _save_easyauth(base_url="", app_key="", credential="")
    notification_settings_adapter.save_dingtalk_channel(
        DingtalkChannelSettingsUpdate(base_url=_BASE, app_key=_APP, credential=_SECRET),
        actor_id=_ACTOR,
    )
    assert notification_settings_adapter.channel_available(CHANNEL_DINGTALK) is True


def test_upstream_dingtalk_notify_probe_warning_when_not_configured() -> None:
    _save_easyauth(base_url="", app_key="", credential="")
    adapter = BlankUpstreamHealthAdapter()
    listed = {item.dependency: item for item in adapter.latest()}
    assert listed["dingtalk_notify"].display_name == "通知服务（钉钉）"
    checked = {item.dependency: item for item in adapter.run_checks(actor_id=_ACTOR)}
    item = checked["dingtalk_notify"]
    assert item.status == "warning"
    assert item.summary_code == "upstream.not_configured"
    assert item.supported is True


def _save_easyauth(*, base_url: str, app_key: str, credential: str) -> None:
    _save_setting(
        "easyauth",
        {
            "base_url": base_url,
            "app_key": app_key,
            "auth_mode": "static_app_token",
            "credential": encrypt_secret(credential) if credential else "",
        },
        actor_id=_ACTOR,
        action="authz.settings.update",
    )
