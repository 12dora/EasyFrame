# 04 · 后端 API 契约（v2 权威版）

挂载点：`/api/v1/local-accounts`（`enterprise_platform` router factory，宿主注入薄 port）。全部 Bearer 鉴权。与已落地 v1 的差异见文末附录。

## 1. 门禁

| 动作 | 所需权限 |
|---|---|
| 列表 / 详情 / 可授目录 | `accounts.local.view` |
| 一切变更 | `accounts.local.manage`，再叠加 §2 矩阵 |

两个 code 可授给非超管本地用户或经 EasyAuth 授给 SSO 用户（委派管理）。所有路由拒绝 `must_change_password=true` 的调用者（403 `PASSWORD_CHANGE_REQUIRED`）。

## 2. 操作者×目标矩阵（守卫核心）

**特权目标** := `is_admin=true` 或持有任一 `high` grant 的账户。

| 操作 | 超管 | 委派管理员（非超管持 manage） |
|---|---|---|
| 创建普通账户 / 授普通（standard）权限 | ✅ | ✅ |
| 创建 `isAdmin=true`；PATCH 升/降 `isAdmin` | ✅ | ❌ 422 |
| grants diff 触及任一 `high` code（增**或**减） | ✅ | ❌ 422 |
| 对**特权目标**的任何变更（资料/启停/删除/改密/TOTP 救援/有效期） | ✅ | ❌ 403 |
| 设置/修改 `expiresAt` | ✅（超管账户本身除外，422） | 仅对非特权目标 |

**对自己（任何人，含超管）**：停用、降级、删除、改有效期、管理面改密、管理面 TOTP 救援一律 403——自助改密/2FA 走 `settings/security`。
**最后管理员不变式**：任何变更后必须仍存在 ≥1 个 active ∧ 未过期 ∧ 本地 ∧ `is_admin` 账户（行锁计数），违者 422。

## 3. 端点

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/local-accounts` | `?search=` | 仅本地行；`{data: Summary[], meta:{total}}` |
| POST | `/local-accounts` | `{username, email?, password, mustChangePassword?=true, isAdmin?=false, permissions?: LocalGrant[], expiresAt?}` | 201→Detail；409 用户名占用（不区分与隐藏 SSO 行的冲突）；422 见 §2 及：空用户名 / `isAdmin=true` 携带非空 permissions / 非法 grant（03 §3.1）/ 过去时间 expiresAt |
| GET | `/local-accounts/{id}` | — | Detail；非本地 id → 404 |
| PATCH | `/local-accounts/{id}` | `{email?, active?, isAdmin?, uiLocale?, expiresAt?}`（expiresAt 三态：缺省=不变/null=清除/值=设置） | §2 矩阵；升超管时清空 grants 并 `local_grants_version`+1 |
| DELETE | `/local-accounts/{id}` | — | 204；§2 矩阵 + 自我 403 + 最后管理员 422；级联删 passkeys/通知/快照行 |
| POST | `/local-accounts/{id}/password` | `{password, mustChangePassword?=true}` | 管理面重置；置 `sessions_revoked_at`；自我 403 |
| PUT | `/local-accounts/{id}/permissions` | `{permissions: LocalGrant[], expectedVersion}` | 整表替换；`expectedVersion` **必填**，CAS 失败 409；422 见 03 §3.1 与 §2 |
| DELETE | `/local-accounts/{id}/totp` | — | 2FA 救援：清双 secret + 吊销会话；自我 403 |
| GET | `/local-accounts/permission-catalog` | — | `{data: [{code, nameZh, nameEn, groupKey, riskLevel, supportedScopes, grantableScopes}]}`，仅 active。`grantableScopes = supportedScopes ∩ {SELF, ALL}`，空数组即不可授本地 |

`LocalGrant = {code, scope}`。
`Summary`: `id, username, email, active, isAdmin, totpEnabled, passkeyCount, mustChangePassword, permissionCount, expiresAt, expired, createdAt`。
`Detail` 增：`uiLocale, permissions: LocalGrant[], baselinePermissions: code[], localGrantsVersion`。

**`/auth/me` 扩展**：增加服务端派生的 `accountId` 与 `isLocalSuperadmin`（02 §1 谓词）——前端能力控件（isAdmin 开关、high 权限勾选、特权目标操作）以此为准，不得从权限集合推断。

## 4. 登录面（契约不变，语义收紧）

`POST /api/v1/auth/login` 端点不变。资格判定见 02 §4，在认证、二次验证、签发与**每次请求**执行；五态模式（各行均隐含 active ∧ 未过期）：

| `BLANK_LOCAL_AUTH_MODE` | 生产 | 允许本地登录 |
|---|---|---|
| `disabled`（默认） | ✅ | 无（OIDC-only） |
| `development` / `demo` | ❌ 启动即炸 | 全部本地账户 |
| `enabled`（新增） | ✅ | 全部本地账户 |
| `break_glass`（收紧） | ✅ | 仅超管 |

- 过期/模式不符的拒绝与"账号或密码错误"同形，不泄露原因。
- **时序**：认证恰验一次 bcrypt（存在取真 hash，否则 dummy），active/过期/模式判定在其后统一执行——消除现存"已知活跃用户名跑两次 hash"的枚举侧信道。
- **seed 启动后置条件**：`disabled` 不 seed；`enabled`/`break_glass` 启动时必须存在可用超管（无则 seed；同名冲突行存在则**启动失败**报错；bootstrap 密码仅在需插入时要求）。
- 三级限流不变（进程内，单副本前提，见 06）。

## 5. 审计

沿用已落地的点分动作名：`accounts.local.create / update / delete / password.reset / permissions.set / totp.disable`（不重命名，保护既有查询）。`permissions.set` 记录 code+scope 前后差集；永不含密码/secret。
新增登录事件：`auth.login.success / failure / second_factor_failure / rate_limited`，携带 mode 与认证方式；`break_glass` 下的 success 是告警信号。
所有变更与其审计行在**同一事务**提交（02 §5 unit-of-work）。

## 6. 错误码

| 码 | 场景 |
|---|---|
| 401 | 无/坏 token；会话吊销；资格判定失败（含过期、模式收紧） |
| 403 | 缺权限；自我危险操作；委派者动特权目标；`PASSWORD_CHANGE_REQUIRED` |
| 404 | id 不存在或非本地账户 |
| 409 | 用户名占用；`expectedVersion` 过期 |
| 422 | 密码策略；非法 grant（未知/停用 code、scope 越界、重复 code）；委派者触及 high/isAdmin；最后管理员；超管携带 grants；超管设有效期；过去时间 |

## 附录 · 与已落地 v1 的差异

grants `code[]` → `LocalGrant[]`（scope 化）；`expectedVersion` 由无到必填；新增 `expiresAt/expired/localGrantsVersion/accountId/isLocalSuperadmin`；catalog 响应增 `nameZh/nameEn/groupKey/supportedScopes/grantableScopes`（去掉 domain/resource 平铺）；新增 §2 矩阵（v1 仅有自我/最后管理员守卫）；登录模式四态→五态；资格判定从仅登录时扩展到每请求。
