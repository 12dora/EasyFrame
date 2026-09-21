"""通知设置声明、端口与生效值解析。存储由宿主实现,内核不碰 ORM。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise_platform.schemas import PlatformModel, StrictPlatformModel

CHANNEL_DINGTALK = "dingtalk"
CHANNEL_IN_APP = "in_app"
NOTIFICATION_CHANNELS = (CHANNEL_DINGTALK, CHANNEL_IN_APP)

Switches = dict[str, dict[str, bool]]
ViewMode = Literal["my", "policy"]


class LocalizedText(BaseModel):
    model_config = ConfigDict(frozen=True)

    zh: str
    en: str


class NotificationScene(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    title: LocalizedText
    description: LocalizedText
    channels: tuple[str, ...] = NOTIFICATION_CHANNELS
    default_enabled: bool = True

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_known_channels(value)


class NotificationGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    title: LocalizedText
    description: LocalizedText
    gate_permission: str
    scenes: tuple[NotificationScene, ...] = Field(min_length=1)


class NotificationCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: tuple[NotificationGroup, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> NotificationCatalog:
        _reject_duplicate_keys([group.key for group in self.groups], "group")
        _reject_duplicate_keys([scene.key for group in self.groups for scene in group.scenes], "scene")
        return self

    def group(self, key: str) -> NotificationGroup | None:
        return next((item for item in self.groups if item.key == key), None)

    def scene(self, key: str) -> tuple[NotificationGroup, NotificationScene] | None:
        for group in self.groups:
            match = next((item for item in group.scenes if item.key == key), None)
            if match is not None:
                return group, match
        return None


class NotificationGroupPolicy(BaseModel):
    managed: bool = True
    switches: Switches = Field(default_factory=dict)


class SwitchChange(BaseModel):
    scene: str
    channel: str
    enabled: bool


class PolicyChange(BaseModel):
    managed: bool | None = None
    switch: SwitchChange | None = None

    @model_validator(mode="after")
    def require_managed_or_switch(self) -> PolicyChange:
        if self.managed is None and self.switch is None:
            raise ValueError("at least one of managed or switch is required")
        return self


class NotificationSettingsPort(Protocol):
    def load_policies(self) -> dict[str, NotificationGroupPolicy]: ...
    def save_policy(self, group_key: str, change: PolicyChange, *, actor_id: str) -> NotificationGroupPolicy: ...
    def load_preferences(self, account_ids: Sequence[str], group_key: str) -> dict[str, Switches]: ...
    def save_preference(self, account_id: str, group_key: str, change: SwitchChange) -> Switches: ...
    def channel_available(self, channel: str) -> bool: ...


class UnknownNotificationTargetError(ValueError):
    """未知分组、场景,或场景未声明该渠道。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class NotificationChannelStatus(PlatformModel):
    key: str
    available: bool


class NotificationSceneView(PlatformModel):
    key: str
    title: LocalizedText
    description: LocalizedText
    channels: dict[str, bool | None]


class NotificationGroupView(PlatformModel):
    key: str
    title: LocalizedText
    description: LocalizedText
    managed: bool
    editable: bool
    scenes: list[NotificationSceneView]


class NotificationSettingsView(PlatformModel):
    can_manage: bool
    channels: list[NotificationChannelStatus]
    groups: list[NotificationGroupView]


class NotificationPolicyView(PlatformModel):
    channels: list[NotificationChannelStatus]
    groups: list[NotificationGroupView]


class NotificationPreferencePatch(StrictPlatformModel):
    group: str
    scene: str
    channel: str
    enabled: bool


class NotificationPolicyPatch(StrictPlatformModel):
    group: str
    managed: bool | None = None
    scene: str | None = None
    channel: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_managed_or_switch(self) -> NotificationPolicyPatch:
        return _complete_policy_patch(self)

    def to_policy_change(self) -> PolicyChange:
        switch = None
        if self.scene is not None and self.channel is not None and self.enabled is not None:
            switch = SwitchChange(scene=self.scene, channel=self.channel, enabled=self.enabled)
        return PolicyChange(managed=self.managed, switch=switch)


def effective_enabled(
    scene: NotificationScene,
    channel: str,
    *,
    managed: bool,
    has_account: bool,
    policy: Switches,
    preference: Switches | None,
) -> bool:
    if managed or not has_account:
        return _first_enabled(scene, channel, policy)
    return _first_enabled(scene, channel, preference or {}, policy)


def build_group_view(
    group: NotificationGroup,
    policy: NotificationGroupPolicy,
    preference: Switches | None,
    *,
    mode: ViewMode,
) -> NotificationGroupView:
    policy_only = mode == "policy"
    return NotificationGroupView(
        key=group.key,
        title=group.title,
        description=group.description,
        managed=policy.managed,
        editable=True if policy_only else not policy.managed,
        scenes=[_scene_view(scene, policy, preference, policy_only=policy_only) for scene in group.scenes],
    )


def validate_change(
    catalog: NotificationCatalog,
    group_key: str,
    scene_key: str,
    channel: str,
) -> tuple[NotificationGroup, NotificationScene]:
    group = catalog.group(group_key)
    if group is None:
        raise UnknownNotificationTargetError("group")
    scene = next((item for item in group.scenes if item.key == scene_key), None)
    if scene is None:
        raise UnknownNotificationTargetError("scene")
    if channel not in NOTIFICATION_CHANNELS or channel not in scene.channels:
        raise UnknownNotificationTargetError("channel")
    return group, scene


def build_my_settings(
    catalog: NotificationCatalog,
    settings: NotificationSettingsPort,
    *,
    account_id: str,
    permissions: Sequence[str],
    can_manage: bool,
) -> NotificationSettingsView:
    policies = settings.load_policies()
    groups = [
        _my_group_view(group, _policy_or_default(policies, group.key), settings, account_id)
        for group in catalog.groups
        if group.gate_permission in permissions
    ]
    return NotificationSettingsView(can_manage=can_manage, channels=channel_statuses(settings), groups=groups)


def build_policy_settings(catalog: NotificationCatalog, settings: NotificationSettingsPort) -> NotificationPolicyView:
    policies = settings.load_policies()
    groups = [
        build_group_view(group, _policy_or_default(policies, group.key), None, mode="policy")
        for group in catalog.groups
    ]
    return NotificationPolicyView(channels=channel_statuses(settings), groups=groups)


def channel_statuses(settings: NotificationSettingsPort) -> list[NotificationChannelStatus]:
    return [
        NotificationChannelStatus(key=channel, available=settings.channel_available(channel))
        for channel in NOTIFICATION_CHANNELS
    ]


def _policy_or_default(policies: dict[str, NotificationGroupPolicy], group_key: str) -> NotificationGroupPolicy:
    return policies.get(group_key, NotificationGroupPolicy())


def _my_group_view(
    group: NotificationGroup,
    policy: NotificationGroupPolicy,
    settings: NotificationSettingsPort,
    account_id: str,
) -> NotificationGroupView:
    preference = None
    if not policy.managed:
        preference = settings.load_preferences((account_id,), group.key).get(account_id)
    return build_group_view(group, policy, preference, mode="my")


def _scene_view(
    scene: NotificationScene,
    policy: NotificationGroupPolicy,
    preference: Switches | None,
    *,
    policy_only: bool,
) -> NotificationSceneView:
    ignore_pref = policy_only or policy.managed
    return NotificationSceneView(
        key=scene.key,
        title=scene.title,
        description=scene.description,
        channels=_channel_states(scene, policy.switches, preference, ignore_pref=ignore_pref),
    )


def _channel_states(
    scene: NotificationScene,
    policy: Switches,
    preference: Switches | None,
    *,
    ignore_pref: bool,
) -> dict[str, bool | None]:
    return {
        channel: _channel_state(scene, channel, policy, preference, ignore_pref=ignore_pref)
        for channel in NOTIFICATION_CHANNELS
    }


def _channel_state(
    scene: NotificationScene,
    channel: str,
    policy: Switches,
    preference: Switches | None,
    *,
    ignore_pref: bool,
) -> bool | None:
    if channel not in scene.channels:
        return None
    return effective_enabled(
        scene, channel, managed=ignore_pref, has_account=True, policy=policy, preference=preference
    )


def _first_enabled(scene: NotificationScene, channel: str, *layers: Switches) -> bool:
    for layer in layers:
        value = _lookup_switch(layer, scene.key, channel)
        if value is not None:
            return value
    return scene.default_enabled


def _lookup_switch(switches: Switches, scene_key: str, channel: str) -> bool | None:
    scene_map = switches.get(scene_key)
    if scene_map is None or channel not in scene_map:
        return None
    return scene_map[channel]


def _require_known_channels(value: tuple[str, ...]) -> tuple[str, ...]:
    unknown = next((item for item in value if item not in NOTIFICATION_CHANNELS), None)
    if unknown is not None:
        raise ValueError(f"unsupported notification channel: {unknown}")
    if len(set(value)) != len(value):
        raise ValueError("duplicate notification channel")
    return value


def _reject_duplicate_keys(keys: list[str], kind: str) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ValueError(f"duplicate notification {kind} key: {key}")
        seen.add(key)


def _complete_policy_patch(patch: NotificationPolicyPatch) -> NotificationPolicyPatch:
    filled = sum(part is not None for part in (patch.scene, patch.channel, patch.enabled))
    if filled not in {0, 3}:
        raise ValueError("scene, channel, and enabled must be provided together")
    if patch.managed is None and filled == 0:
        raise ValueError("at least one of managed or switch is required")
    return patch
