"""blank host notification settings: tables, adapter, HTTP, migration."""

from __future__ import annotations

import importlib
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from blank_app.adapter_notification_settings import notification_settings_adapter
from blank_app.database import SessionLocal, engine
from blank_app.models import Account, PlatformAuditLog, PlatformNotificationPolicy, PlatformNotificationPreference
from enterprise_platform.notification_settings import (
    CHANNEL_DINGTALK,
    CHANNEL_IN_APP,
    NotificationGroupManagedError,
    SwitchChange,
)
from platform_tests.test_blank_app_api import _login, _reset_admin

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

PREFIX = "/api/v1/notification-settings"
GROUP = "member"
SCENE = "account.security_alert"
POLICY_AUDIT = "notification.policy.update"
PREF_AUDIT = "notification.preferences.update"


@pytest.fixture(autouse=True)
def _clean_notification_rows(request):
    if "upgrade_and_downgrade" in request.node.name:
        yield
        return
    _wipe_rows()
    yield
    _wipe_rows()


@pytest.fixture
def _unconfigured_easyauth():
    from blank_app.adapter_support import _get_setting, _save_setting

    previous = _get_setting("easyauth")
    try:
        _save_setting("easyauth", {}, actor_id="test-actor", action="authz.settings.update")
        yield
    finally:
        _save_setting("easyauth", previous, actor_id="test-actor", action="authz.settings.update")


def test_model_metadata_matches_notification_settings_schema() -> None:
    policy = PlatformNotificationPolicy.__table__
    assert list(policy.primary_key.columns.keys()) == ["group_key"]
    assert policy.c.group_key.type.length == 80
    assert policy.c.managed.nullable is False
    assert str(policy.c.switches.server_default.arg) == "{}"
    pref = PlatformNotificationPreference.__table__
    assert list(pref.primary_key.columns.keys()) == ["account_id", "group_key"]
    assert {item.ondelete for item in pref.c.account_id.foreign_keys} == {"CASCADE"}


def test_notification_settings_revision_follows_ui_preferences() -> None:
    module = importlib.import_module("blank_app.alembic.versions.0007_notification_settings")
    assert module.revision == "0007_notification_settings"
    assert module.down_revision == "0006_ui_preferences"


def test_notification_settings_upgrade_and_downgrade() -> None:
    config = Config("blank_app/alembic.ini")
    try:
        engine.dispose()
        command.downgrade(config, "0006_ui_preferences")
        names = _table_names()
        assert "platform_notification_policies" not in names
        assert "platform_notification_preferences" not in names
        engine.dispose()
        command.upgrade(config, "head")
        names = _table_names()
        assert "platform_notification_policies" in names
        assert "platform_notification_preferences" in names
        fks = _preference_foreign_keys()
        assert any((item.get("ondelete") or (item.get("options") or {}).get("ondelete")) == "CASCADE" for item in fks)
    finally:
        engine.dispose()
        command.upgrade(config, "head")
        engine.dispose()


@pytest.mark.usefixtures("_unconfigured_easyauth")
def test_defaults_with_no_rows() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _auth(client)
        response = client.get(PREFIX, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["canManage"] is True
        assert body["channels"] == [
            {"key": CHANNEL_DINGTALK, "available": False},
            {"key": CHANNEL_IN_APP, "available": True},
        ]
        assert [group["key"] for group in body["groups"]] == ["member", "administrator"]
        for group in body["groups"]:
            assert group["managed"] is True
            assert group["editable"] is False
            for scene in group["scenes"]:
                assert scene["channels"] == {CHANNEL_DINGTALK: True, CHANNEL_IN_APP: True}


def test_policy_patch_changes_one_switch_and_audits() -> None:
    from blank_app.main import app

    with SessionLocal() as db:
        db.add(
            PlatformNotificationPolicy(
                group_key=GROUP,
                managed=True,
                switches={SCENE: {CHANNEL_IN_APP: True, "legacy": True}, "unknown.scene": {CHANNEL_DINGTALK: False}},
            )
        )
        db.commit()
    with TestClient(app) as client:
        headers = _auth(client)
        response = client.patch(f"{PREFIX}/policy", headers=headers, json=_switch_body(enabled=False))
        assert response.status_code == 200, response.text
        assert response.json()["scenes"][0]["channels"] == {CHANNEL_DINGTALK: True, CHANNEL_IN_APP: False}
        assert response.json()["scenes"][1]["channels"][CHANNEL_IN_APP] is True
    with SessionLocal() as db:
        row = db.get(PlatformNotificationPolicy, GROUP)
        assert row is not None
        assert row.switches[SCENE][CHANNEL_IN_APP] is False
        assert row.switches[SCENE]["legacy"] is True
        assert row.switches["unknown.scene"] == {CHANNEL_DINGTALK: False}
        audit = _latest_audit(db, POLICY_AUDIT)
        assert audit.before_data["switches"][SCENE][CHANNEL_IN_APP] is True
        assert audit.after_data["switches"][SCENE][CHANNEL_IN_APP] is False
        assert audit.after_data["switches"][SCENE]["legacy"] is True


def test_preference_patch_conflict_when_managed() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _auth(client)
        response = client.patch(f"{PREFIX}/preferences", headers=headers, json=_switch_body(enabled=False))
        assert response.status_code == 409
        assert response.json()["detail"] == {"code": "notification_group_managed"}


def test_adapter_save_preference_managed_writes_nothing() -> None:
    account_id = _admin_id()
    with SessionLocal() as db:
        db.add(PlatformNotificationPolicy(group_key=GROUP, managed=True, switches={}))
        db.commit()
    with pytest.raises(NotificationGroupManagedError) as captured:
        notification_settings_adapter.save_preference(
            account_id, GROUP, SwitchChange(scene=SCENE, channel=CHANNEL_IN_APP, enabled=False)
        )
    assert captured.value.group_key == GROUP
    with SessionLocal() as db:
        assert db.get(PlatformNotificationPreference, (uuid.UUID(account_id), GROUP)) is None
        assert db.query(PlatformAuditLog).filter(PlatformAuditLog.action == PREF_AUDIT).first() is None


def test_managed_cycle_keeps_and_restores_preferences() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _auth(client)
        policy = f"{PREFIX}/policy"
        assert client.patch(policy, headers=headers, json={"group": GROUP, "managed": False}).status_code == 200
        saved = client.patch(f"{PREFIX}/preferences", headers=headers, json=_switch_body(enabled=False))
        assert saved.status_code == 200, saved.text
        assert _mine_switch(client, headers) is False
        assert client.patch(policy, headers=headers, json={"group": GROUP, "managed": True}).status_code == 200
        hosted = _group(client.get(PREFIX, headers=headers).json())
        assert hosted["managed"] is True
        assert hosted["editable"] is False
        assert hosted["scenes"][0]["channels"][CHANNEL_IN_APP] is True
        with SessionLocal() as db:
            row = db.get(PlatformNotificationPreference, (uuid.UUID(_admin_id()), GROUP))
            assert row is not None
            assert row.switches[SCENE][CHANNEL_IN_APP] is False
            assert _latest_audit(db, PREF_AUDIT).after_data["switches"][SCENE][CHANNEL_IN_APP] is False
        assert client.patch(policy, headers=headers, json={"group": GROUP, "managed": False}).status_code == 200
        restored = _group(client.get(PREFIX, headers=headers).json())
        assert restored["editable"] is True
        assert restored["scenes"][0]["channels"][CHANNEL_IN_APP] is False


def test_account_deletion_cascades_preferences() -> None:
    account_id, _headers = _create_member()
    parsed = uuid.UUID(account_id)
    with SessionLocal() as db:
        db.add(
            PlatformNotificationPreference(
                account_id=parsed, group_key=GROUP, switches={SCENE: {CHANNEL_IN_APP: False}}
            )
        )
        db.commit()
    try:
        with SessionLocal() as db:
            account = db.get(Account, parsed)
            assert account is not None
            db.delete(account)
            db.commit()
        with SessionLocal() as db:
            assert db.get(PlatformNotificationPreference, (parsed, GROUP)) is None
            assert db.get(Account, parsed) is None
    finally:
        _delete_account(account_id)


def test_policy_forbidden_without_manage_permission() -> None:
    from blank_app.main import app

    account_id, headers = _create_member()
    try:
        with TestClient(app) as client:
            assert client.get(f"{PREFIX}/policy", headers=headers).status_code == 403
            patch = client.patch(f"{PREFIX}/policy", headers=headers, json={"group": GROUP, "managed": False})
            assert patch.status_code == 403
    finally:
        _delete_account(account_id)


def _wipe_rows() -> None:
    with SessionLocal() as db:
        db.query(PlatformNotificationPreference).delete(synchronize_session=False)
        db.query(PlatformNotificationPolicy).delete(synchronize_session=False)
        db.query(PlatformAuditLog).filter(PlatformAuditLog.action.in_((POLICY_AUDIT, PREF_AUDIT))).delete(
            synchronize_session=False
        )
        db.commit()


def _auth(client: TestClient) -> dict[str, str]:
    _reset_admin()
    login = _login(client)
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['accessToken']}"}


def _switch_body(*, enabled: bool) -> dict[str, object]:
    return {"group": GROUP, "scene": SCENE, "channel": CHANNEL_IN_APP, "enabled": enabled}


def _group(body: dict, key: str = GROUP) -> dict:
    return next(item for item in body["groups"] if item["key"] == key)


def _mine_switch(client: TestClient, headers: dict[str, str]) -> bool:
    return _group(client.get(PREFIX, headers=headers).json())["scenes"][0]["channels"][CHANNEL_IN_APP]


def _admin_id() -> str:
    with SessionLocal() as db:
        account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        return str(account.id)


def _latest_audit(db, action: str) -> PlatformAuditLog:
    row = (
        db.query(PlatformAuditLog)
        .filter(PlatformAuditLog.action == action)
        .order_by(PlatformAuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    return row


def _create_member() -> tuple[str, dict[str, str]]:
    from blank_app.adapters import account_adapter, pwd_context

    account = Account(
        username=f"ns-{uuid.uuid4().hex[:12]}",
        password_hash=pwd_context.hash("member-password-43!"),
        active=True,
        is_admin=False,
        must_change_password=False,
    )
    with SessionLocal() as db:
        db.add(account)
        db.commit()
        account_id = str(account.id)
    return account_id, {"Authorization": f"Bearer {account_adapter.issue_session(account_id)}"}


def _delete_account(account_id: str) -> None:
    parsed = uuid.UUID(account_id)
    with SessionLocal() as db:
        db.query(PlatformNotificationPreference).filter_by(account_id=parsed).delete(synchronize_session=False)
        account = db.get(Account, parsed)
        if account is not None:
            db.delete(account)
        db.commit()


def _table_names() -> set[str]:
    with engine.connect() as connection:
        return set(inspect(connection).get_table_names())


def _preference_foreign_keys() -> list[dict]:
    with engine.connect() as connection:
        return inspect(connection).get_foreign_keys("platform_notification_preferences")
