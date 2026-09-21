"""plan_delivery 批量读端口与未知场景错误。"""

from __future__ import annotations

import pytest

from enterprise_platform.notification_dispatch import (
    NotificationRecipient,
    UnknownNotificationSceneError,
    plan_delivery,
)
from enterprise_platform.notification_settings import CHANNEL_DINGTALK, CHANNEL_IN_APP, NotificationGroupPolicy
from platform_tests.notification_settings_fakes import (
    SCENE_RESULT,
    MemoryNotificationSettings,
    sample_catalog,
)


def test_plan_delivery_unknown_scene_is_an_error() -> None:
    with pytest.raises(UnknownNotificationSceneError) as captured:
        plan_delivery(sample_catalog(), MemoryNotificationSettings(), "missing.scene", ())
    assert captured.value.scene_key == "missing.scene"


def test_plan_delivery_batches_preference_loads_when_unmanaged() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)
    recipients = (
        NotificationRecipient(ref="r1", account_id="a1"),
        NotificationRecipient(ref="r2", account_id="a2"),
        NotificationRecipient(ref="r3", account_id=None),
        NotificationRecipient(ref="r4", account_id="a1"),
    )
    plan = plan_delivery(sample_catalog(), settings, SCENE_RESULT, recipients)
    assert settings.policy_loads == 1
    assert settings.preference_loads == [(("a1", "a2"), "learner")]
    assert [item.ref for item in plan.in_app] == ["r1", "r2"]
    assert [item.ref for item in plan.dingtalk] == ["r1", "r2", "r3", "r4"]


def test_plan_delivery_skips_preferences_when_managed() -> None:
    settings = MemoryNotificationSettings()
    settings.preferences[("a1", "learner")] = {SCENE_RESULT: {CHANNEL_IN_APP: False, CHANNEL_DINGTALK: False}}
    recipients = (NotificationRecipient(ref="r1", account_id="a1"),)
    plan = plan_delivery(sample_catalog(), settings, SCENE_RESULT, recipients)
    assert settings.policy_loads == 1
    assert settings.preference_loads == []
    assert [item.ref for item in plan.in_app] == ["r1"]
    assert [item.ref for item in plan.dingtalk] == ["r1"]


def test_plan_delivery_uses_unmanaged_preferences() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)
    settings.preferences[("a1", "learner")] = {SCENE_RESULT: {CHANNEL_IN_APP: False, CHANNEL_DINGTALK: True}}
    recipients = (
        NotificationRecipient(ref="r1", account_id="a1"),
        NotificationRecipient(ref="r2", account_id="a2"),
    )
    plan = plan_delivery(sample_catalog(), settings, SCENE_RESULT, recipients)
    assert settings.preference_loads == [(("a1", "a2"), "learner")]
    assert [item.ref for item in plan.in_app] == ["r2"]
    assert [item.ref for item in plan.dingtalk] == ["r1", "r2"]


def test_plan_delivery_dedupes_in_app_by_account_id_preserving_order() -> None:
    settings = MemoryNotificationSettings()
    settings.policies["learner"] = NotificationGroupPolicy(managed=False)
    recipients = (
        NotificationRecipient(ref="r1", account_id="a1"),
        NotificationRecipient(ref="r2", account_id="a2"),
        NotificationRecipient(ref="r3", account_id="a1"),
        NotificationRecipient(ref="r4", account_id="a3"),
    )
    plan = plan_delivery(sample_catalog(), settings, SCENE_RESULT, recipients)
    assert [item.ref for item in plan.in_app] == ["r1", "r2", "r4"]
    assert [item.account_id for item in plan.in_app] == ["a1", "a2", "a3"]
    assert [item.ref for item in plan.dingtalk] == ["r1", "r2", "r3", "r4"]


def test_plan_delivery_dedupes_dingtalk_by_ref_preserving_order() -> None:
    settings = MemoryNotificationSettings()
    recipients = (
        NotificationRecipient(ref="r1", account_id="a1"),
        NotificationRecipient(ref="r1", account_id="a2"),
        NotificationRecipient(ref="r2", account_id=None),
        NotificationRecipient(ref="r3", account_id="a3"),
        NotificationRecipient(ref="r2", account_id="a4"),
    )
    plan = plan_delivery(sample_catalog(), settings, SCENE_RESULT, recipients)
    assert [item.ref for item in plan.dingtalk] == ["r1", "r2", "r3"]
    assert [item.account_id for item in plan.in_app] == ["a1", "a2", "a3", "a4"]
