"""通知设置测试用内存端口与装配夹具。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.notification_settings import (
    CHANNEL_IN_APP,
    NOTIFICATION_CHANNELS,
    DingtalkChannelSettings,
    DingtalkChannelSettingsUpdate,
    LocalizedText,
    NotificationCatalog,
    NotificationGroup,
    NotificationGroupManagedError,
    NotificationGroupPolicy,
    NotificationScene,
    PolicyChange,
    SwitchChange,
    Switches,
)
from enterprise_platform.schemas import ConnectionTestResult, CurrentUser

LEARNER_GATE = "exam.learner.view"
TEACHER_GATE = "exam.teacher.view"
SCENE_RESULT = "exam.result_released"
SCENE_REMINDER = "exam.reminder"
SCENE_SUBMITTED = "exam.submitted"
PREFIX = "/api/v1/notification-settings"


def loc(zh: str, en: str) -> LocalizedText:
    return LocalizedText(zh=zh, en=en)


def sample_catalog() -> NotificationCatalog:
    return NotificationCatalog(
        groups=(
            NotificationGroup(
                key="learner",
                title=loc("学员", "Learner"),
                description=loc("学员通知", "Learner notices"),
                gate_permission=LEARNER_GATE,
                scenes=(
                    NotificationScene(
                        key=SCENE_RESULT,
                        title=loc("成绩发布", "Results released"),
                        description=loc("考试成绩", "Exam results"),
                    ),
                    NotificationScene(
                        key=SCENE_REMINDER,
                        title=loc("考试提醒", "Exam reminder"),
                        description=loc("即将开考", "Upcoming exam"),
                        channels=(CHANNEL_IN_APP,),
                        default_enabled=False,
                    ),
                ),
            ),
            NotificationGroup(
                key="teacher",
                title=loc("教师", "Teacher"),
                description=loc("教师通知", "Teacher notices"),
                gate_permission=TEACHER_GATE,
                scenes=(
                    NotificationScene(
                        key=SCENE_SUBMITTED,
                        title=loc("交卷", "Submitted"),
                        description=loc("学员交卷", "Learner submitted"),
                    ),
                ),
            ),
        )
    )


def settings_user(*permissions: str, user_id: str = "user-1") -> CurrentUser:
    return CurrentUser(id=user_id, name="alice", must_change_password=False, permissions=list(permissions))


def copy_switches(switches: Switches) -> Switches:
    return {scene: dict(channels) for scene, channels in switches.items()}


class MemoryNotificationSettings:
    def __init__(self) -> None:
        self.policies: dict[str, NotificationGroupPolicy] = {}
        self.preferences: dict[tuple[str, str], Switches] = {}
        self.unavailable: set[str] = set()
        self.policy_loads = 0
        self.preference_loads: list[tuple[tuple[str, ...], str]] = []
        self.before_save_preference: Callable[[], None] | None = None
        self.dingtalk = DingtalkChannelSettings(
            base_url="",
            app_key="",
            base_url_inherited=False,
            app_key_inherited=False,
            has_credential=False,
            credential_source="none",
            configured=True,
            updated_at=None,
        )
        self.dingtalk_secret = ""
        self.dingtalk_test = ConnectionTestResult(ok=True, latency_ms=12)

    def load_policies(self) -> dict[str, NotificationGroupPolicy]:
        self.policy_loads += 1
        return {
            key: NotificationGroupPolicy(managed=item.managed, switches=copy_switches(item.switches))
            for key, item in self.policies.items()
        }

    def save_policy(self, group_key: str, change: PolicyChange, *, actor_id: str) -> NotificationGroupPolicy:
        del actor_id
        current = self.policies.get(group_key, NotificationGroupPolicy())
        managed = current.managed if change.managed is None else change.managed
        switches = _apply_switch(copy_switches(current.switches), change.switch)
        updated = NotificationGroupPolicy(managed=managed, switches=switches)
        self.policies[group_key] = updated
        return updated

    def load_preferences(self, account_ids: Sequence[str], group_key: str) -> dict[str, Switches]:
        self.preference_loads.append((tuple(account_ids), group_key))
        loaded: dict[str, Switches] = {}
        for account_id in account_ids:
            stored = self.preferences.get((account_id, group_key))
            if stored is not None:
                loaded[account_id] = copy_switches(stored)
        return loaded

    def save_preference(self, account_id: str, group_key: str, change: SwitchChange) -> Switches:
        if self.before_save_preference is not None:
            self.before_save_preference()
        policy = self.policies.setdefault(group_key, NotificationGroupPolicy())
        if policy.managed:
            raise NotificationGroupManagedError(group_key)
        key = (account_id, group_key)
        updated = _apply_switch(copy_switches(self.preferences.get(key, {})), change)
        self.preferences[key] = updated
        return copy_switches(updated)

    def channel_available(self, channel: str) -> bool:
        return channel not in self.unavailable and channel in NOTIFICATION_CHANNELS

    def load_dingtalk_channel(self) -> DingtalkChannelSettings:
        return self.dingtalk

    def save_dingtalk_channel(
        self, payload: DingtalkChannelSettingsUpdate, *, actor_id: str
    ) -> DingtalkChannelSettings:
        del actor_id
        updates: dict[str, object] = {
            "configured": self.channel_available("dingtalk"),
            "updated_at": datetime.now(UTC),
        }
        if payload.base_url is not None:
            updates["base_url"] = payload.base_url
            updates["base_url_inherited"] = payload.base_url == ""
        if payload.app_key is not None:
            updates["app_key"] = payload.app_key
            updates["app_key_inherited"] = payload.app_key == ""
        incoming = payload.plain_credential()
        if incoming is not None:
            self.dingtalk_secret = incoming
            updates["has_credential"] = bool(incoming)
            updates["credential_source"] = "settings" if incoming else "none"
        self.dingtalk = self.dingtalk.model_copy(update=updates)
        return self.dingtalk

    def test_dingtalk_channel(self) -> ConnectionTestResult:
        return self.dingtalk_test


class _UserPort:
    def __init__(self, user: CurrentUser) -> None:
        self._user = user

    def current_user(self) -> CurrentUser:
        return self._user


def make_settings_client(
    *,
    user: CurrentUser,
    settings: MemoryNotificationSettings | None = None,
    catalog: NotificationCatalog | None = None,
    with_ports: bool = True,
) -> TestClient:
    port = settings or MemoryNotificationSettings()
    declared = catalog or sample_catalog()
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=_UserPort(user),
                app_settings=None,
                notifications=None,
                integrations=None,
                directory=None,
                upstream_health=None,
                require_permission=lambda _code: None,
                notification_settings=port if with_ports else None,
                notification_catalog=declared if with_ports else None,
            ),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                passkeys=False,
                footer=False,
                notifications=False,
                identity=False,
                easyauth=False,
                upstream=False,
            ),
        ),
        prefix="/api/v1",
    )
    return TestClient(app)


def preference_body(*, enabled: bool, scene: str = SCENE_RESULT, channel: str = CHANNEL_IN_APP) -> dict[str, object]:
    return {"group": "learner", "scene": scene, "channel": channel, "enabled": enabled}


def group_payload(body: dict[str, object], key: str = "learner") -> dict[str, object]:
    groups = body["groups"]
    assert isinstance(groups, list)
    return next(item for item in groups if item["key"] == key)


def _apply_switch(switches: Switches, change: SwitchChange | None) -> Switches:
    if change is None:
        return switches
    scene_map = dict(switches.get(change.scene, {}))
    scene_map[change.channel] = change.enabled
    switches[change.scene] = scene_map
    return switches
