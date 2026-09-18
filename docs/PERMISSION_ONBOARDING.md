# 无业务权限引导页

登录成功但没有任何可用功能的账号，应看到「如何申请权限」，而不是设置页上的「当前账号没有访问此页面的权限。」判定与整页引导由 EasyUI 提供，申请地址由共享 `GET /api/v1/auth/session` 提供；宿主只接线。

## 后端

`GET /api/v1/auth/session` 与 `PATCH /api/v1/auth/preferences`（`enterprise_platform` 账户路由，与 `/auth/me` 同组 `PlatformRouteGroups.me`）：

- **只校验登录**，不校验权限码（零授权用户必须能读到）。
- `GET /auth/session` 响应：

```json
{
  "permissionRequestUrl": "<EasyAuth 申请地址或 null>",
  "preferences": { "tableDensity": "compact" }
}
```

  - `permissionRequestUrl` 来自 `PlatformSetting["easyauth"].permission_request_url`（`EasyAuthStatus.permission_request_url`）。未配置、空白或集成未接时为 `null`。
  - `preferences.tableDensity` 为 `"compact"` | `"comfortable"`，来自账号 `platform_accounts.ui_preferences.table_density`；键缺失或非法值时全局缺省 `"compact"`。
- `PATCH /auth/preferences` 请求体为 `{ "tableDensity"?: "compact" | "comfortable" }`（`StrictPlatformModel`，只接受 camelCase）。未知键或非法值 422。成功返回更新后的 `AuthSession`。变更写入 `PlatformAuditLog`（`auth.preferences.update`）。
- 不带回接入状态或密钥。

blank_app 经 `create_platform_router` 自动挂载，无需再写一条宿主路由。

## 宿主接入清单

子模块更新后（EasyFrame + EasyUI 两个指针都要升），镜像宿主对照 blank：

### 后端

- 使用带 `/auth/session` 与 `/auth/preferences` 的 `enterprise_platform`；不要在宿主再复制这两条路由。
- 已有路由权限清单测试的宿主，升指针时把 `("GET", "/api/v1/auth/session")` 与 `("PATCH", "/api/v1/auth/preferences")` 加入已审阅的「只校验登录」名单。EasyCustoms：`backend/customs_tests/test_route_permission_manifest.py` 的 `_REVIEWED_AUTH_ONLY`。
- 宿主若已自建 `/auth/session`（EasyLearning：`learning_app/api/session.py`），必须删掉：Starlette 先匹配到的那条会静默挡住后挂上的共享路由。
- `/auth/session` 与 `/auth/preferences` 挂在路由组 `me`。`me=False` 的宿主（EasyTrade web 自建 `/auth/me`）拿不到共享路由，必须自己提供 `permissionRequestUrl` 与 `preferences.tableDensity`。
- **平台表必须与 blank 一致**：宿主复制 blank 的 alembic 迁移 `0006_ui_preferences`，给 `platform_accounts` 增加 `ui_preferences`（JSON，`NOT NULL`，server default `{}`）；账户模型加同名列；AccountPort 实现 `get_ui_preferences` / `update_ui_preferences`。JSON 内键为 `table_density`（`compact` | `comfortable`）。
- 零授权用户的 EasyAuth `permission_request_url` 仍走既有设置面（`authz-integration/settings`）。

### 前端模板（`frontend/apps/blank`）

- 升 EasyUI 到含 `EnterprisePermissionOnboarding` / `hasEnterpriseBusinessAccess` 的提交。
- `lib/permissions.ts` — 把**带门禁的导航与设置面板**权限码列成 `BLANK_BUSINESS_PERMISSION_CODES`（空白站：`identity.integration.view`、`authz.integration.view`、`accounts.local.view`、`ops.upstream_health.view`、`settings.app_setting.update`）。通知中心不要列入。
- `lib/shell-adapter.ts` — `loadAuthSession()` → `GET /api/v1/auth/session`，落地成 `ShellSession { permissionRequestUrl, tableDensity }`；失败/超时/`""` 一律回缺省（无入口 + 紧凑），且 `preserveSessionOn401`（会话对错只由 `/auth/me` 裁决）。外壳走 `startShellIdentityLoad()`：两条请求同 tick 发出，只 `await` `/auth/me`，这两项后台补（见 [SHELL_PERCEIVED_LOADING.md](SHELL_PERCEIVED_LOADING.md)）。
- `preferences.tableDensity` 的接线（外壳上的 `TableDensityProvider`、设置 → 外观、`PATCH /auth/preferences` 的乐观写回）见 [TABLE_DENSITY.md](TABLE_DENSITY.md)，本文只管申请入口。
- `components/blank-shell.tsx` — 身份加载完成后，强制改密页仍优先；否则当 `!hasEnterpriseBusinessAccess({ permissions, securityCapabilities, isLocalSuperadmin, businessPermissionCodes: BLANK_BUSINESS_PERMISSION_CODES })` 时，**整页渲染 `EnterprisePermissionOnboarding`，不要放进 `EnterpriseAppFrame`**。`businessPermissionCodes` 必填。`/auth/session` 与 `/auth/me` 同 tick 发出但不阻塞外壳；**只有引导页**在 `onboardingReady(identity, permissionUrlPending)` 为假时继续画骨架屏，避免申请链接在首屏之后才出现。
- `onRecheck` 接 `useShellIdentity` 的 `refreshIdentity`（立即重取 `/auth/me`）；`onLogout` 接到 `performEnterpriseLogout`；`permissionRequestUrl` 读 `identity.permissionRequestUrl`；文案用 `createEnterpriseLabelCatalog` 的 `permissionOnboarding`。
- `firstAuthorizedAppPath` 一类登录后落点**不要改**；无权限改的是外壳，不是落点。

EasyUI 导出名：`EnterprisePermissionOnboarding`、`EnterprisePermissionOnboardingProps`、`EnterprisePermissionOnboardingIdentity`、`EnterprisePermissionOnboardingLabels`、`hasEnterpriseBusinessAccess`、`EnterpriseBusinessAccessInput`。接线说明见 EasyUI `docs/PERMISSION-ONBOARDING.md`。
