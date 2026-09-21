"""通知设置目录、生效值与变更校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise_platform.notification_settings import (
    CHANNEL_DINGTALK,
    CHANNEL_IN_APP,
    NOTIFICATION_CHANNELS,
    LocalizedText,
    NotificationCatalog,
    NotificationGroup,
    NotificationGroupManagedError,
    NotificationGroupPolicy,
    NotificationScene,
    PolicyChange,
    SwitchChange,
    UnknownNotificationTargetError,
    build_group_view,
    effective_enabled,
    validate_change,
)
from platform_tests.notification_settings_fakes import (
    LEARNER_GATE,
    SCENE_REMINDER,
    SCENE_RESULT,
    MemoryNotificationSettings,
    loc,
    sample_catalog,
)

_SCENE = NotificationScene(
    key=SCENE_RESULT,
    title=LocalizedText(zh="成绩", en="Result"),
    description=LocalizedText(zh="成绩", en="Result"),
)


def _group(*scenes: NotificationScene, key: str = "learner") -> NotificationGroup:
    return NotificationGroup(
        key=key,
        title=loc(key, key),
        description=loc(key, key),
        gate_permission=LEARNER_GATE,
        scenes=scenes,
    )


def test_catalog_rejects_duplicate_group_and_scene_keys() -> None:
    scene = _SCENE
    with pytest.raises(ValidationError, match="duplicate notification group key"):
        NotificationCatalog(groups=(_group(scene), _group(scene)))
    other = NotificationScene(
        key=SCENE_RESULT,
        title=loc("x", "x"),
        description=loc("x", "x"),
    )
    with pytest.raises(ValidationError, match="duplicate notification scene key"):
        NotificationCatalog(groups=(_group(scene), _group(other, key="teacher")))


def test_catalog_rejects_unknown_channel_and_empty_collections() -> None:
    with pytest.raises(ValidationError, match="unsupported notification channel"):
        NotificationScene(key="s", title=loc("s", "s"), description=loc("s", "s"), channels=("email",))
    with pytest.raises(ValidationError):
        NotificationCatalog(groups=())
    with pytest.raises(ValidationError):
        _group()


def test_catalog_lookup_group_and_scene() -> None:
    catalog = sample_catalog()
    assert catalog.group("learner") is not None
    assert catalog.group("missing") is None
    located = catalog.scene(SCENE_REMINDER)
    assert located is not None
    group, scene = located
    assert group.key == "learner"
    assert scene.channels == (CHANNEL_IN_APP,)
    assert catalog.scene("missing") is None


@pytest.mark.parametrize(
    ("managed", "has_account", "pref", "policy", "expected"),
    [
        (True, True, True, False, False),
        (True, True, False, True, True),
        (True, True, True, None, True),
        (True, False, True, False, False),
        (False, True, True, False, True),
        (False, True, False, True, False),
        (False, True, None, False, False),
        (False, True, None, None, True),
        (False, False, True, False, False),
    ],
)
def test_effective_enabled_matrix(
    managed: bool, has_account: bool, pref: bool | None, policy: bool | None, expected: bool
) -> None:
    policy_sw = {} if policy is None else {SCENE_RESULT: {CHANNEL_DINGTALK: policy}}
    pref_sw = None if pref is None else {SCENE_RESULT: {CHANNEL_DINGTALK: pref}}
    assert (
        effective_enabled(
            _SCENE,
            CHANNEL_DINGTALK,
            managed=managed,
            has_account=has_account,
            policy=policy_sw,
            preference=pref_sw,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("group", "scene", "channel", "kind"),
    [
        ("missing", SCENE_RESULT, CHANNEL_IN_APP, "group"),
        ("learner", "missing.scene", CHANNEL_IN_APP, "scene"),
        ("learner", SCENE_REMINDER, CHANNEL_DINGTALK, "channel"),
        ("learner", SCENE_RESULT, "sms", "channel"),
    ],
)
def test_validate_change_unknown_group_scene_or_channel(group: str, scene: str, channel: str, kind: str) -> None:
    with pytest.raises(UnknownNotificationTargetError) as captured:
        validate_change(sample_catalog(), group, scene, channel)
    assert captured.value.kind == kind


def test_policy_change_requires_managed_or_switch() -> None:
    with pytest.raises(ValidationError, match="managed or switch"):
        PolicyChange()
    PolicyChange(managed=False)
    PolicyChange(switch=SwitchChange(scene=SCENE_RESULT, channel=CHANNEL_IN_APP, enabled=True))


def test_save_preference_rejects_managed_group() -> None:
    settings = MemoryNotificationSettings()
    change = SwitchChange(scene=SCENE_RESULT, channel=CHANNEL_IN_APP, enabled=False)
    with pytest.raises(NotificationGroupManagedError) as captured:
        settings.save_preference("user-1", "learner", change)
    assert captured.value.group_key == "learner"
    assert ("user-1", "learner") not in settings.preferences


def test_group_view_my_and_policy_modes() -> None:
    catalog = sample_catalog()
    group = catalog.group("learner")
    assert group is not None
    policy = NotificationGroupPolicy(
        managed=False, switches={SCENE_RESULT: {CHANNEL_DINGTALK: False, CHANNEL_IN_APP: True}}
    )
    preference = {SCENE_RESULT: {CHANNEL_IN_APP: False}}
    mine = build_group_view(group, policy, preference, mode="my")
    assert mine.editable is True
    assert mine.scenes[0].channels == {CHANNEL_DINGTALK: False, CHANNEL_IN_APP: False}
    assert mine.scenes[1].channels == {CHANNEL_DINGTALK: None, CHANNEL_IN_APP: False}
    hosted = build_group_view(group, policy.model_copy(update={"managed": True}), preference, mode="my")
    assert hosted.editable is False
    assert hosted.scenes[0].channels == {CHANNEL_DINGTALK: False, CHANNEL_IN_APP: True}
    platform = build_group_view(group, policy, preference, mode="policy")
    assert platform.editable is True
    assert platform.managed is False
    assert platform.scenes[0].channels == {CHANNEL_DINGTALK: False, CHANNEL_IN_APP: True}
    assert set(NOTIFICATION_CHANNELS) == {CHANNEL_DINGTALK, CHANNEL_IN_APP}
