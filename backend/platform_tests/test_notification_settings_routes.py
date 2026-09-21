"""通知设置 HTTP 路由:门控、生效值、冲突与权限。"""

from __future__ import annotations

from enterprise_platform.assembly.contracts import NOTIFICATION_SETTINGS_MANAGE
from enterprise_platform.notification_settings import CHANNEL_DINGTALK, CHANNEL_IN_APP, NotificationGroupPolicy
from platform_tests.notification_settings_fakes import (
    LEARNER_GATE,
    PREFIX,
    SCENE_REMINDER,
    SCENE_RESULT,
    TEACHER_GATE,
    MemoryNotificationSettings,
    group_payload,
    make_settings_client,
    preference_body,
    settings_user,
)


def test_get_mine_lists_gated_groups_with_effective_values() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(
        managed=False,
        switches={SCENE_RESULT: {CHANNEL_DINGTALK: False}},
    )
    settings.preferences[("user-1", "learner")] = {SCENE_RESULT: {CHANNEL_IN_APP: False}}
    client = make_settings_client(user=settings_user(LEARNER_GATE), settings=settings)
    response = client.get(PREFIX)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["canManage"] is False
    assert body["channels"] == [
        {"key": CHANNEL_DINGTALK, "available": True},
        {"key": CHANNEL_IN_APP, "available": True},
    ]
    assert [group["key"] for group in body["groups"]] == ["learner"]
    learner = body["groups"][0]
    assert learner["managed"] is False
    assert learner["editable"] is True
    assert learner["scenes"][0]["channels"] == {CHANNEL_DINGTALK: False, CHANNEL_IN_APP: False}
    assert learner["scenes"][1]["channels"] == {CHANNEL_DINGTALK: None, CHANNEL_IN_APP: False}


def test_patch_preferences_conflict_when_managed() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE))
    response = client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False))
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "notification_group_managed"}


def test_patch_preferences_conflict_when_managed_between_check_and_save() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)

    def flip_to_managed() -> None:
        settings.policies["learner"] = NotificationGroupPolicy(managed=True)

    settings.before_save_preference = flip_to_managed
    client = make_settings_client(user=settings_user(LEARNER_GATE), settings=settings)
    response = client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False))
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "notification_group_managed"}
    assert ("user-1", "learner") not in settings.preferences


def test_patch_preferences_forbidden_without_gate() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)
    client = make_settings_client(user=settings_user(TEACHER_GATE), settings=settings)
    response = client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False))
    assert response.status_code == 403


def test_patch_preferences_unknown_scene_not_found() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE))
    body = preference_body(enabled=True, scene="missing.scene")
    assert client.patch(f"{PREFIX}/preferences", json=body).status_code == 404


def test_patch_preferences_unsupported_channel_not_found() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE))
    body = preference_body(enabled=True, scene=SCENE_REMINDER, channel=CHANNEL_DINGTALK)
    assert client.patch(f"{PREFIX}/preferences", json=body).status_code == 404


def test_patch_preferences_success_returns_updated_group() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)
    client = make_settings_client(user=settings_user(LEARNER_GATE), settings=settings)
    response = client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"] == "learner"
    assert body["editable"] is True
    assert body["scenes"][0]["channels"][CHANNEL_IN_APP] is False


def test_policy_endpoints_forbidden_without_manage_permission() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE))
    assert client.get(f"{PREFIX}/policy").status_code == 403
    patch = client.patch(f"{PREFIX}/policy", json={"group": "learner", "managed": False})
    assert patch.status_code == 403


def test_get_policy_lists_all_groups_with_platform_values() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(
        managed=True, switches={SCENE_RESULT: {CHANNEL_DINGTALK: False}}
    )
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE), settings=settings)
    response = client.get(f"{PREFIX}/policy")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [group["key"] for group in body["groups"]] == ["learner", "teacher"]
    learner = group_payload(body)
    assert learner["editable"] is True
    assert learner["managed"] is True
    assert learner["scenes"][0]["channels"][CHANNEL_DINGTALK] is False
    assert all(group["editable"] is True for group in body["groups"])


def test_patch_policy_updates_group() -> None:
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE))
    response = client.patch(
        f"{PREFIX}/policy",
        json={
            "group": "learner",
            "managed": False,
            "scene": SCENE_RESULT,
            "channel": CHANNEL_DINGTALK,
            "enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["managed"] is False
    assert body["editable"] is True
    assert body["scenes"][0]["channels"][CHANNEL_DINGTALK] is False


def test_toggle_managed_off_restores_dormant_preferences() -> None:
    settings = MemoryNotificationSettings()
    client = make_settings_client(user=settings_user(LEARNER_GATE, NOTIFICATION_SETTINGS_MANAGE), settings=settings)
    policy = f"{PREFIX}/policy"
    assert client.patch(policy, json={"group": "learner", "managed": False}).status_code == 200
    saved = client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False))
    assert saved.status_code == 200
    assert client.patch(policy, json={"group": "learner", "managed": True}).status_code == 200
    hosted = group_payload(client.get(PREFIX).json())
    assert hosted["managed"] is True
    assert hosted["editable"] is False
    assert hosted["scenes"][0]["channels"][CHANNEL_IN_APP] is True
    assert client.patch(policy, json={"group": "learner", "managed": False}).status_code == 200
    restored = group_payload(client.get(PREFIX).json())
    assert restored["managed"] is False
    assert restored["editable"] is True
    assert restored["scenes"][0]["channels"][CHANNEL_IN_APP] is False


def test_routes_absent_when_ports_not_provided() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE), with_ports=False)
    assert client.get(PREFIX).status_code == 404
    assert client.get(f"{PREFIX}/policy").status_code == 404
    assert client.patch(f"{PREFIX}/preferences", json=preference_body(enabled=False)).status_code == 404
    assert client.get(f"{PREFIX}/channels/dingtalk").status_code == 404


def test_dingtalk_channel_forbidden_without_manage_permission() -> None:
    client = make_settings_client(user=settings_user(LEARNER_GATE))
    path = f"{PREFIX}/channels/dingtalk"
    assert client.get(path).status_code == 403
    assert client.put(path, json={"baseUrl": "https://easyauth.example.test"}).status_code == 403
    assert client.post(f"{path}/test").status_code == 403


def test_dingtalk_channel_get_put_hides_credential() -> None:
    secret = "eat_notify_secret_value"
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE))
    path = f"{PREFIX}/channels/dingtalk"
    loaded = client.get(path)
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["hasCredential"] is False
    assert body["credentialSource"] == "none"
    assert "credential" not in body
    saved = client.put(
        path,
        json={"baseUrl": "https://easyauth.example.test", "appKey": "enterprise-blank", "credential": secret},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["baseUrl"] == "https://easyauth.example.test"
    assert body["appKey"] == "enterprise-blank"
    assert body["hasCredential"] is True
    assert body["credentialSource"] == "settings"
    assert "credential" not in body
    assert secret not in saved.text


def test_dingtalk_channel_test_uses_port() -> None:
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE))
    response = client.post(f"{PREFIX}/channels/dingtalk/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["latencyMs"] == 12


def test_dingtalk_channel_rejects_invalid_base_url() -> None:
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE))
    response = client.put(f"{PREFIX}/channels/dingtalk", json={"baseUrl": "not-a-url"})
    assert response.status_code == 422


def test_dingtalk_channel_put_omits_secret_from_422() -> None:
    secret = "s" * 4001
    client = make_settings_client(user=settings_user(NOTIFICATION_SETTINGS_MANAGE))
    response = client.put(f"{PREFIX}/channels/dingtalk", json={"credential": secret})
    assert response.status_code == 422
    assert secret not in response.text
