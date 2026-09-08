# EasyAuth 授权事件接入

EasyAuth 在用户当前授权或应用权限目录变更后，向下游 `POST /api/v1/easyauth/events` 推送签名事件。下游必须验签、立即刷新对应用户快照，并在登录时强制拉取，避免最多 300 秒的缓存窗口里继续看到旧权限。

## 端点

`POST /api/v1/easyauth/events`，无需登录。请求头：

- `X-EasyAuth-Event`
- `X-EasyAuth-Delivery`
- `X-EasyAuth-Timestamp`（Unix 秒，与服务器时差不超过 300 秒）
- `X-EasyAuth-Signature`：`hex(HMAC-SHA256(webhook_secret, "{timestamp}.{raw_body}"))`

验签失败返回 `401 {"error":"invalid_signature"}`。`X-EasyAuth-Event` 必须与 JSON 体 `event_type` 一致。

| event_type | 处理 |
|---|---|
| `webhook.test` | `200 {"ok": true}` |
| `grant.changed` | 按 `user_id` 强制拉取权限快照；本地已是 `snapshot_version` 时幂等成功 |
| `catalog.changed` | 将该 `app_key` 下全部缓存快照标为过期，下次登录或权限检查再拉 |
| 其他 | `422 {"error":"unsupported_event"}` |

权限检查遇到过期缓存会懒刷新。刷新失败时 **fail-closed**：视为零权限，不沿用过期行（与原先过期即拒绝一致）。登录强制拉取失败时保留最后一次成功快照，没有缓存才是零权限。

覆盖守卫按 `(grant_version, catalog_version)` 比较，目录-only 变更也能写入。

## 密钥

`PlatformSetting` 键 `easyauth` 增加 `webhook_secret`，与 `credential` 一样用 envelope 加密存储。管理端通过既有 `PUT /api/v1/authz-integration/settings` 写入，JSON 字段：

- `webhookSecret`：明文密钥，省略则保留已存值
- 响应 `hasWebhookSecret`

前端（本仓未改）：`frontend/apps/blank/lib/authorization-adapter.ts` 的 `BlankEasyAuthSettings` / `BlankEasyAuthSettingsUpdate`，以及 EasyUI 授权设置表单，需增加与 `credential` 同形态的 `webhookSecret` / `hasWebhookSecret` 输入。

## 宿主接入清单

子模块更新后，宿主除使用新的 `enterprise_platform` 外，需镜像以下 blank_app 文件（`_facade()` 拆分后的实现）：

- `blank_app/authz_snapshot.py` — 强制/懒拉取、版本守卫、webhook 处理函数
- `blank_app/oidc_adapter.py` — 登录 `ensure_account_snapshot(..., force=True)`
- `blank_app/authz_api.py` — `BlankAuthorizationOperations` 的 webhook 端口方法；可信 principal 登录同样 `force=True`
- `blank_app/adapter_account.py` — 权限检查走 `snapshot_grants_for_account`
- `blank_app/adapter_platform.py` — `webhook_secret` 存取、`get_easyauth_webhook_secret`
- `blank_app/adapter_support.py` — 审计脱敏包含 `webhook_secret`
- `blank_app/adapters.py` — 再导出
- `blank_app/authz_descriptor.py` — `webhook.events_url`
- `blank_app/main.py` — `PlatformPorts.authorization`，以及 `/api/v1/easyauth` 的 `Cache-Control: no-store`

共享层要求：

- `PlatformPorts.authorization` 指向实现了 `refresh_snapshot_for_external_user` / `invalidate_app_snapshots` 的 `AuthorizationOperationsPort`
- `IntegrationPort.get_easyauth_webhook_secret()` 返回明文 webhook 密钥
- 清单 `webhook.signing` 保持 `hmac-sha256`，并声明 `"events_url": "/api/v1/easyauth/events"`（相对路径，EasyAuth 按应用 `base_url` 解析）
