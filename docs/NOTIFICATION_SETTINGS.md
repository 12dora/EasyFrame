# 通知设置框架(Notification Settings)

下游应用只**声明**通知场景并调用框架判定;开关存储、平台托管、个人偏好、HTTP 接口与设置页由框架统一提供,各应用不得自建一套。

## 1. 模型

- **渠道(channel)**:`dingtalk`(钉钉工作通知,经 EasyAuth notify 发送)、`in_app`(站内通知,`platform_notifications`)。渠道 id 是数据,线上不做驼峰转换。
- **场景(scene)**:一类业务事件,如 `exam.result_released`。每个场景声明支持的渠道与默认值。
- **角色分组(group)**:一组面向同一角色的场景,如 `learner`。分组用**权限码**(`gate_permission`)判定可见性——与 EasyAuth 授权模型一致:运行时只认权限码,不认角色显示名。分组 key 建议与 EasyAuth 授权组 key 对齐。
- **平台托管(managed)**:按分组生效。开启时该分组一律按平台值发送,成员只读;关闭时成员可自定义,未自定义的开关回落平台值。个人设置在托管期间**保留但不生效**,关闭托管后自动恢复。
- 新装默认:所有分组 `managed = true`,所有开关取场景声明的默认值(默认开启)。

生效值:

```
managed 或收件人无账号:  policy[scene][channel] ?? scene.default
否则:                   preference[scene][channel] ?? policy[scene][channel] ?? scene.default
```

## 2. 声明(宿主代码即事实来源)

`enterprise_platform/notification_settings.py`:

```python
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_IN_APP = "in_app"
NOTIFICATION_CHANNELS = (CHANNEL_DINGTALK, CHANNEL_IN_APP)

class LocalizedText(BaseModel):        # frozen
    zh: str
    en: str

class NotificationScene(BaseModel):    # frozen, extra=forbid
    key: str                           # 分组内外全局唯一,形如 "exam.result_released"
    title: LocalizedText
    description: LocalizedText
    channels: tuple[str, ...] = NOTIFICATION_CHANNELS
    default_enabled: bool = True

class NotificationGroup(BaseModel):    # frozen
    key: str
    title: LocalizedText
    description: LocalizedText
    gate_permission: str
    scenes: tuple[NotificationScene, ...]

class NotificationCatalog(BaseModel):  # frozen;构造时校验 key 唯一、渠道合法、分组非空
    groups: tuple[NotificationGroup, ...]
    def group(self, key) -> NotificationGroup | None
    def scene(self, key) -> tuple[NotificationGroup, NotificationScene] | None
```

权限码 `notification.settings.manage`(常量 `NOTIFICATION_SETTINGS_MANAGE`,与 `NOTIFICATION_CENTER_VIEW` 同处定义)控制「平台配置」。宿主把它登记进自己的权限目录并放入合适的授权组。

## 3. 端口(宿主实现,内核不碰 ORM)

```python
Switches = dict[str, dict[str, bool]]          # {scene_key: {channel: enabled}},稀疏,仅存覆盖值

class NotificationGroupPolicy(BaseModel):
    managed: bool = True
    switches: Switches = {}

class NotificationSettingsPort(Protocol):
    def load_policies(self) -> dict[str, NotificationGroupPolicy]: ...            # 缺行 = 默认
    def save_policy(self, group_key: str, change: PolicyChange, *, actor_id: str) -> NotificationGroupPolicy: ...
    def load_preferences(self, account_ids: Sequence[str], group_key: str) -> dict[str, Switches]: ...
    def save_preference(self, account_id: str, group_key: str, change: SwitchChange) -> Switches: ...
    def channel_available(self, channel: str) -> bool: ...                        # 钉钉未配置时 False

class SwitchChange(BaseModel):  scene: str; channel: str; enabled: bool
class PolicyChange(BaseModel):  managed: bool | None = None; switch: SwitchChange | None = None   # 至少一项
class NotificationGroupManagedError(Exception):  # 携带 group_key
```

`save_policy` 必须在同一事务内对策略行加锁(`SELECT ... FOR UPDATE`,缺行先插入)后读改写,只改补丁携带的字段;写审计 `notification.policy.update`(before/after)。

`save_preference` 必须在同一事务内先对**该分组策略行**加同样的行锁(缺行先插入),托管则抛 `NotificationGroupManagedError`(携带 `group_key`)且不得写偏好;否则再锁偏好行并只改补丁携带的那一个开关,写审计 `notification.preferences.update`(before/after)。HTTP 层可做托管预检,但不得只靠预检。

`PlatformPorts.notification_settings: NotificationSettingsPort | None = None`,`notification_catalog: NotificationCatalog | None = None`;二者任缺其一则不注册路由(老宿主不受影响)。

存储(宿主 Alembic 建表,两宿主同构):

| 表 | 列 |
|---|---|
| `platform_notification_policies` | `group_key` String(80) PK, `managed` bool 默认 true, `switches` JSON, `updated_at`, `updated_by` |
| `platform_notification_preferences` | `account_id` FK→accounts(级联删除) + `group_key` String(80) 复合 PK, `switches` JSON, `updated_at` |

## 4. HTTP(前缀 `/api/v1/notification-settings`)

分组对象(两处响应通用):

```json
{
  "key": "learner",
  "title": {"zh": "学员", "en": "Learner"},
  "description": {"zh": "…", "en": "…"},
  "managed": true,
  "editable": false,
  "scenes": [
    {"key": "exam.result_released", "title": {…}, "description": {…},
     "channels": {"dingtalk": true, "in_app": true}}
  ]
}
```

`channels` 的值为 `true/false`;场景不支持的渠道为 `null`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `` | 登录 | `{canManage, channels:[{key, available}], groups:[…]}`。仅含当前用户持有 `gate_permission` 的分组;开关为**本人生效值**;`editable = !managed` |
| PATCH | `/preferences` | 登录 + 分组 gate | 体 `{group, scene, channel, enabled}` → 更新后的分组对象。托管中(含预检通过后、写入前策略被改为托管,端口抛 `NotificationGroupManagedError`) → HTTP 409,FastAPI 信封 `{"detail": {"code": "notification_group_managed"}}`(与其它内核编码错误相同,`code` 在 `detail` 里);未知分组/场景/渠道 → 404;无 gate → 403 |
| GET | `/policy` | `notification.settings.manage` | `{channels, groups}`:全部分组,开关为**平台值**,`editable = true` |
| PATCH | `/policy` | `notification.settings.manage` | 体 `{group, managed?, scene?, channel?, enabled?}`(`managed` 与开关三元组至少给一项)→ 更新后的分组对象 |

## 5. 发送判定

`enterprise_platform/notification_dispatch.py`(纯函数 + 端口读取,不做 IO 发送):

```python
@dataclass(frozen=True)
class NotificationRecipient:
    ref: str                 # 宿主的收件人引用(目录 user_ref / local:<uuid>)
    account_id: str | None   # 无账号者只能按平台值判定,且收不到站内通知

@dataclass(frozen=True)
class DeliveryPlan:
    scene_key: str
    in_app: tuple[NotificationRecipient, ...]     # 仅含有账号者
    dingtalk: tuple[NotificationRecipient, ...]

def plan_delivery(catalog, settings: NotificationSettingsPort, scene_key: str,
                  recipients: Sequence[NotificationRecipient]) -> DeliveryPlan
```

先对原始收件人列表按出现顺序去重(一次扫描:若 `ref` 已出现,或其非空 `account_id` 已出现,则视为重复,只保留第一次),再在去重后的列表上判定各渠道。未声明的 `scene_key` 抛 `UnknownNotificationSceneError`(编程错误,不静默吞)。宿主拿到计划后:站内通知与业务写入同事务;钉钉一律在事务 / advisory lock 之外发送,失败不回滚业务。

## 6. 前端(EasyUI)

`EnterpriseNotificationSettingsSurface({ adapter, labels, locale })`,适配器:

```ts
type NotificationChannel = "dingtalk" | "in_app";
interface NotificationSettingsAdapter {
  load(): Promise<NotificationSettingsView>;                 // GET ``
  savePreference(change: NotificationSwitchChange): Promise<NotificationGroupView>;
  loadPolicy(): Promise<NotificationPolicyView>;             // GET /policy
  savePolicy(change: NotificationPolicyChange): Promise<NotificationGroupView>;
}
```

- `canManage` 为真时页面顶部出现分段切换「我的通知 / 平台配置」;否则只有「我的通知」。
- 每个分组一张卡片:标题行右端是「平台托管」开关(「我的通知」里恒为只读,仅展示状态);正文是场景表格,列 = 通知场景 / 钉钉 / 站内通知。托管中的分组在「我的通知」里整表置灰只读,但开关状态照常可见。
- 开关即点即存(无保存按钮):乐观更新,失败回滚并提示;同一开关在途期间禁用。

## 7. 宿主接入清单

1. 声明 `NotificationCatalog`;权限目录登记 `notification.settings.manage`。
2. 建两张表(Alembic),实现 `NotificationSettingsPort`,装入 `PlatformPorts`。
3. 业务事件处调用 `plan_delivery`,按计划写站内通知 / 发钉钉。
4. 前端新增 `settings/notifications` 页面挂载 EasyUI surface,设置导航增加「通知」,`routes.spec` 补中英文路由。

blank 是参考实现(目录只供设置页演示,不接线真实发送):

| 步骤 | blank 指针 |
|---|---|
| 目录 | `backend/blank_app/notification_catalog.py` |
| 权限 | `backend/blank_app/permission_registry.py`(`notification.settings.manage`,与 `notification.center.view` 并列;显示名在 `authz_snapshot.seed_platform_catalog`) |
| 表 | `backend/blank_app/models.py` 的 `PlatformNotificationPolicy` / `PlatformNotificationPreference`;迁移 `backend/blank_app/alembic/versions/0007_notification_settings.py` |
| 端口 | `backend/blank_app/adapter_notification_settings.py`,经 `adapters.py` 装入 `backend/blank_app/main.py` 的 `PlatformPorts` |
| 设置页 | `frontend/apps/blank/app/[locale]/app/settings/notifications/page.tsx`(外壳导航「通知」) |
