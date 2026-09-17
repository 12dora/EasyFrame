# EasyAuth 授权事件接入

EasyAuth 在用户当前授权或应用权限目录变更后，向下游 `POST /api/v1/easyauth/events` 推送签名事件。下游必须验签、立即刷新对应用户快照，并在登录时强制拉取，避免最多 300 秒的缓存窗口里继续看到旧权限。

## 端点

`POST /api/v1/easyauth/events`，无需登录。请求头：

- `X-EasyAuth-Event`
- `X-EasyAuth-Delivery`
- `X-EasyAuth-Timestamp`（Unix 秒，最多 10 位十进制；与服务器时差不超过 300 秒）
- `X-EasyAuth-Signature`：`hex(HMAC-SHA256(webhook_secret, "{timestamp}.{raw_body}"))`，64 位 ASCII hex

验签失败（含畸形时间戳、非 ASCII/非 hex 签名）返回 `401 {"error":"invalid_signature"}`。`X-EasyAuth-Event` 必须与 JSON 体 `event_type` 一致。端点在协程里读 body 并完成验签，验签通过后再把同步的库访问/快照拉取放到 `run_in_threadpool`。

`grant.changed` / `catalog.changed` 的 `app_key` 必须与本地配置一致，缺失返回 `422 {"error":"invalid_payload"}`，不匹配返回 `422 {"error":"app_key_mismatch"}`。

| event_type | 处理 |
|---|---|
| `webhook.test` | `200 {"ok": true}` |
| `grant.changed` | 按 `user_id` 强制拉取权限快照；本地已是 `snapshot_version` 时幂等成功 |
| `catalog.changed` | 将该 `app_key` 在当前 EasyAuth authority 下的最低接受 `catalog_version` 抬高并过期缓存；已记录的最低版本 >= 宣布版本时幂等跳过 |
| 其他 | `422 {"error":"unsupported_event"}` |

`grant.changed` 在 `app_key` 校验通过后，缺失 `user_id` / `snapshot_version` 仍返回 `422 {"error":"invalid_payload"}`，不进入拉取。

权限检查遇到过期缓存会懒刷新。刷新失败时 **fail-closed**：视为零权限，不沿用过期行（与原先过期即拒绝一致），并在约 30 秒内不再重试上游；同一 `external_user_id` 的并发检查只允许一次在飞拉取。登录强制拉取失败时保留最后一次成功快照，没有缓存才是零权限。

可信 principal（网关每请求注入）只做懒刷新，缓存未过期时不打 EasyAuth。强制拉取只发生在真正的登录边界（OIDC 回调 `upsert_identity`）以及 `grant.changed`。强制拉取必须等待同一用户的在飞拉取结束，再核验 `snapshot_version` / `(grant_version, catalog_version)`；未满足期望则自己再拉一次，不得把事件前开始的在飞结果当作已刷新。

覆盖守卫按 `(grant_version, catalog_version)` 比较，目录-only 变更也能写入。目录下限与快照写入在同一事务：先锁下限行，再锁快照行后复检，并用 `catalog_version >= floor` 条件写入。缓存读取时 `catalog_version` 低于下限的行视为未命中，避免 catalog.changed 之前开始的拉取用新过期时间把过期授权重新武装。下限抬升是 `GREATEST` 原子 upsert；已记录版本 >= 宣布版本时 no-op。

## 密钥

`PlatformSetting` 键 `easyauth` 增加 `webhook_secret`，与 `credential` 一样用 envelope 加密存储。管理端通过既有 `PUT /api/v1/authz-integration/settings` 写入，JSON 字段：

- `webhookSecret`：明文密钥，省略则保留已存值
- 响应 `hasWebhookSecret`

最低接受目录版本存在独立键 `easyauth_catalog_floor`（JSON：`{authority: {app_key: catalog_version}}`，`authority` 为已配置的 EasyAuth `base_url`；仍兼容旧的 `{app_key: catalog_version}`），不走设置表单。切换 EasyAuth `base_url` 时重置该键，避免指向另一套 EasyAuth 后仍按旧下限拒绝新快照。

前端（本仓未改）：`frontend/apps/blank/lib/authorization-adapter.ts` 的 `BlankEasyAuthSettings` / `BlankEasyAuthSettingsUpdate`，以及 EasyUI 授权设置表单，需增加与 `credential` 同形态的 `webhookSecret` / `hasWebhookSecret` 输入。

## 宿主接入清单

子模块更新后，宿主除使用新的 `enterprise_platform` 外，需镜像以下 blank_app 文件（`_facade()` 拆分后的实现）：

- `blank_app/authz_catalog_floor.py` — 目录最低版本：按 EasyAuth authority+app_key 作用域、GREATEST 原子抬升、与快照写入同事务加锁、authority 变更时重置
- `blank_app/authz_snapshot.py` — 强制/懒拉取、版本守卫、失败退避与单飞、webhook 处理函数；写入与缓存读取都受目录下限约束；强制拉取等待在飞结果并核验版本
- `blank_app/oidc_adapter.py` — 登录 `ensure_account_snapshot(..., force=True)`
- `blank_app/authz_api.py` — `BlankAuthorizationOperations` 的 webhook 端口方法；可信 principal 走懒刷新（`force=False`）
- `blank_app/adapter_account.py` — 权限检查走 `snapshot_grants_for_account`
- `blank_app/adapter_platform.py` — `webhook_secret` 存取、`get_easyauth_webhook_secret`、EasyAuth `base_url` 变更时重置目录下限
- `blank_app/adapter_support.py` — 审计脱敏包含 `webhook_secret`
- `blank_app/adapters.py` — 再导出
- `blank_app/authz_descriptor.py` — `webhook.events_url`
- `blank_app/main.py` — `PlatformPorts.authorization`，以及 `/api/v1/easyauth` 的 `Cache-Control: no-store`

共享层要求：

- `PlatformPorts.authorization` 指向实现了 `refresh_snapshot_for_external_user` / `invalidate_app_snapshots(app_key, catalog_version)` 的 `AuthorizationOperationsPort`
- `IntegrationPort.get_easyauth_webhook_secret()` 返回明文 webhook 密钥；`get_easyauth_status().app_key` 用于入站事件的 app_key 校验
- 清单 `webhook.signing` 保持 `hmac-sha256`，并声明 `"events_url": "/api/v1/easyauth/events"`（相对路径，EasyAuth 按应用 `base_url` 解析）

## 认证热路径辅助与装配合同

共享库提供 ORM 无关辅助，宿主自己管 `Account` / `PermissionSnapshot` / `Session`。

| 模块 | 公开 API |
|---|---|
| `enterprise_platform.request_scope` | `RequestScopeMiddleware`（纯 ASGI）、`request_scope() -> dict \| None`、`drop_request_memo(*keys)`、`begin_request_scope` / `end_request_scope` |
| `enterprise_platform.ttl_cache` | `TtlBox(ttl_seconds)`：`peek() -> (hit, value)`、`load(loader)` 单飞（in-flight Future，loader 不持锁；generation 已变则不写入；异常唤醒等待者且不缓存）、`set(value, *, generation=)`（generation 不匹配则 no-op）、`invalidate()` 丢值并 `generation += 1` |
| `enterprise_platform.authz.snapshot_freshness` | `SnapshotFreshness`、`classify_snapshot`、`invalidated_expires_at`、`BackgroundRefresher`（按模块路径 import，不从 `authz` 包再导出） |

`request_scope()` 在请求外是 `None`。ContextVar 会拷进线程池：调用方只改返回的 dict，不要 `ContextVar.set` 新 dict。

### 快照新鲜度

`classify_snapshot(fetched_at, expires_at, now, *, near_expiry_ratio=0.40, stale_grace=10min)`：naive datetime 视为 UTC。`expires_at <= fetched_at`（显式失效或倒置窗口）一律 `EXPIRED`，即使 `now < expires_at`（时钟回偏也不能把已吊销行当成 FRESH）。

| 状态 | 条件 | 宿主请求路径 |
|---|---|---|
| `FRESH` | `expires_at > fetched_at` 且 `now < expires_at` 且剩余寿命 ≥ 40% 窗口 | 用行，不打网 |
| `NEAR_EXPIRY` | `expires_at > fetched_at` 且 `now < expires_at` 且剩余寿命 < `0.40 * (expires_at - fetched_at)` | 用行，后台刷新，key `"{app_key}:{external_user_id}"`，遵守既有失败退避 |
| `STALE_GRACE` | `now >= expires_at` 且 `expires_at > fetched_at` 且 `now < expires_at + 10min` | 用行的 grants（仍受 catalog floor / `catalog_version` 约束；低于下限的行永不使用），后台刷新；请求不得等 EasyAuth |
| `EXPIRED` | `expires_at <= fetched_at`，或宽限已过，或无行 | 同步拉取；失败 fail-closed 为零授权，保留宿主已有 last-good-row |

**显式失效**（`catalog.changed`、管理员吊销等）必须 `expires_at = fetched_at`（`invalidated_expires_at(fetched_at)`），该行永远不能进宽限，也不得判为 `FRESH`。`grant.changed` 仍走既有强制同步刷新。EasyAuth HTTP 拉取期间不得持有 DB session。

`BackgroundRefresher(max_workers=2, name="authz-refresh")`：`schedule(key, fn) -> bool` 按 key 去重，已在飞或已 `shutdown`（含 submit 与 shutdown 竞态）返回 `False`、不抛错；done-callback 一律清 key，意外异常 `logger.error(..., exc_info=...)`；已完成 Future 只留 WeakSet 给测试用 `wait(timeout=)`。`shutdown()` 取消未完成任务、不等待。每个 uvicorn worker 各自一份进程内调度，这是预期。

### `require_permission(..., user=)`

默认 `permission_for(code)` 依赖与 `AssemblyDependencies.current_user` **同一个 callable**。路由同时 `Depends(ctx.current_user)` 和 `Depends(ctx.permission_for(code))` 时，`AccountPort.current_user` 只跑一次。

宿主 port：

```python
def require_permission(code: str, request: Request | None = None, *, user: CurrentUser | None = None) -> None:
    ...
```

传入 `user=` 时**不得**再解析当前用户：检查 `code in user.permissions`，写宿主 `authorization.denied` 审计，并 `raise AuthError(403, "缺少权限")`。框架在 port 接受 `user` 时传入已解析用户；旧的 `require_permission(code)` / `(code, request)` 仍可用（拒绝路径可能仍会二次解析）。传入 `permission_dependency_factory` 的宿主行为不变。`create_platform_router(...)` 签名不变。

### 宿主接入清单

子模块更新后（blank / EasyLearning / EasyCustoms / EasyTrade 各改自己的入口，不要改共享库里没有的文件）：

1. **中间件**：所有 `@app.middleware("http")` 写完之后 `app.add_middleware(RequestScopeMiddleware)`，让它包在 `BaseHTTPMiddleware` 之外。
2. **安全突变**（签发 / 吊销会话、改密）调用 `drop_request_memo("current_user", "account")`（或宿主自己的 key）。
3. **Workers**：后端默认 2 个 uvicorn worker，环境变量 `BLANK_WEB_WORKERS` / `LEARNING_WEB_WORKERS`（EasyCustoms 已有 `CUSTOMS_WEB_WORKERS`），整数 ≥1，非法值启动失败。`uvicorn.run("pkg.main:app", workers=n, ...)` 必须用 import-string。进程内限流 / SSE / websocket 注册表 / 内存锁按 worker 各算一份；跨进程正确性靠 PostgreSQL advisory lock，不要假设单进程。
4. 实现上一节的新鲜度语义与 `require_permission(..., user=)`。
5. `TtlBox` 只缓存近静态行（目录 / EasyAuth 设置 / catalog floor），不要缓存账号、快照或 grants。
