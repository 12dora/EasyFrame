# 无业务权限引导页

登录成功但没有任何可用功能的账号，应看到「如何申请权限」，而不是设置页上的「当前账号没有访问此页面的权限。」判定与整页引导由 EasyUI 提供，申请地址由共享 `GET /api/v1/auth/session` 提供；宿主只接线。

## 后端

`GET /api/v1/auth/session`（`enterprise_platform` 账户路由，与 `/auth/me` 同组 `PlatformRouteGroups.me`）：

- **只校验登录**，不校验权限码（零授权用户必须能读到）。
- 响应 `{ "permissionRequestUrl": "<EasyAuth 申请地址或 null>" }`，来自 `PlatformSetting["easyauth"].permission_request_url`（`EasyAuthStatus.permission_request_url`）。未配置、空白或集成未接时为 `null`。
- 不带回接入状态或密钥。

blank_app 经 `create_platform_router` 自动挂载，无需再写一条宿主路由。

## 宿主接入清单

子模块更新后（EasyFrame + EasyUI 两个指针都要升），镜像宿主对照 blank：

### 后端

- 使用带 `/auth/session` 的 `enterprise_platform`；不要在宿主再复制一条 session 路由。
- 零授权用户的 EasyAuth `permission_request_url` 仍走既有设置面（`authz-integration/settings`）。

### 前端模板（`frontend/apps/blank`）

- 升 EasyUI 到含 `EnterprisePermissionOnboarding` / `hasEnterpriseBusinessAccess` 的提交。
- `lib/permissions.ts` — 把**带门禁的导航与设置面板**权限码列成 `BLANK_BUSINESS_PERMISSION_CODES`（空白站：`identity.integration.view`、`authz.integration.view`、`accounts.local.view`、`ops.upstream_health.view`、`settings.app_setting.update`）。通知中心不要列入。
- `lib/shell-adapter.ts` — `loadAuthSession()` → `GET /api/v1/auth/session`；失败/超时/`""` 一律 `null`，且 `preserveSessionOn401`（会话对错只由 `/auth/me` 裁决）。
- `components/blank-shell.tsx` — 身份加载完成后，强制改密页仍优先；否则当 `!hasEnterpriseBusinessAccess({ permissions, securityCapabilities, isLocalSuperadmin, businessPermissionCodes: BLANK_BUSINESS_PERMISSION_CODES })` 时，**整页渲染 `EnterprisePermissionOnboarding`，不要放进 `EnterpriseAppFrame`**。
- `onRecheck` 只重拉 `/auth/me`（`loadShellIdentity`）；`onLogout` 接到 `performEnterpriseLogout`；`permissionRequestUrl` 来自 `loadAuthSession`；文案用 `createEnterpriseLabelCatalog` 的 `permissionOnboarding`。
- `firstAuthorizedAppPath` 一类登录后落点**不要改**；无权限改的是外壳，不是落点。

EasyUI 导出名：`EnterprisePermissionOnboarding`、`EnterprisePermissionOnboardingProps`、`EnterprisePermissionOnboardingIdentity`、`EnterprisePermissionOnboardingLabels`、`hasEnterpriseBusinessAccess`、`EnterpriseBusinessAccessInput`。接线说明见 EasyUI `docs/PERMISSION-ONBOARDING.md`。
