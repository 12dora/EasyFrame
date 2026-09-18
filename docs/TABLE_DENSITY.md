# 表格密度（按账号保存的行高偏好）

列表页的行高不是某一张表的事：同一个人在不同列表之间来回切，行高必须处处一致，否则每换一页
都要重新对焦。所以档位是一份**账号偏好**（紧凑 / 宽松，缺省紧凑），存在服务端而不是浏览器里
——换台机器、换个浏览器，列表还是自己习惯的那一档，也就不会出现「本地存了一份、服务端存了
另一份」的两个事实源。

判定与界面都在 EasyUI（`TableDensityProvider` / `EnterpriseAppearanceSettingsSurface`），
宿主只接线。

## 线合同

值随身份一起到，改档单独写回：

- `GET /api/v1/auth/session` → `preferences.tableDensity`：`"compact"` | `"comfortable"`。
  字段缺失、为 `null` 或是不认得的值，一律按契约默认值 `"compact"` 读（**契约水合，不是兼容
  垫片**：后端补齐这个字段之前，前端也必须给出一个确定的默认）。
- `PATCH /api/v1/auth/preferences`，请求体 `{ "tableDensity": "compact" | "comfortable" }`，
  成功返回更新后的 `AuthSession`（与 `GET /auth/session` 同一个形状）。

两条路由都只校验登录、不校验权限码，由 `enterprise_platform` 提供；后端细节与宿主后端清单见
[PERMISSION_ONBOARDING.md](PERMISSION_ONBOARDING.md)。

## 前端接线（blank 模板里已经做好的四处）

1. **身份带着档位**（`lib/shell-adapter.ts`）：`ShellIdentity.tableDensity`；`/auth/session` 落地成
   `ShellSession { permissionRequestUrl, tableDensity }`（`tableDensityOf()` 负责水合）；
   写回是 `saveTableDensity(next)` → `PATCH /auth/preferences`，返回更新后的会话。
   `/auth/me` 交出的身份先拿默认值，等 `/auth/session` 后台补——外壳不为这条附属请求多等一毫秒。
2. **Provider 挂在「身份可用」那一层**（`components/table-density.tsx` +
   `components/blank-shell.tsx` 的 `BlankShellIdentityProvider`）：身份 context 外面套一层
   `TableDensityProvider`，外壳每包一次身份，行高就只接一次。写回是**乐观**的：点下去表格立刻
   换档（等一趟往返再变会让人以为没点上），失败回滚到身份里的那个值并 `toast.error`。
   成功后把新档位写回身份快照（`writeCachedIdentity`）——不然下一次身份复查落地时，那份还没
   刷新过的旧偏好会把刚改的档位顶回去。
3. **设置 → 外观**（`app/[locale]/app/settings/appearance/page.tsx`）：整页就是
   `EnterpriseAppearanceSettingsSurface`（自带 `PageHeader`，本页唯一的 H1）。它只读写上面那份
   上下文，自己既不取数也不落盘，所以「设置页改一下」与「所有列表页的行高」天然是同一个事实源。
   **没有权限门禁**：只改当前账号自己的偏好，任何登录用户都进得去，导航项因此无条件出现
   （`components/blank-shell.tsx` 的 `settingsItems`，排在「通用」之后）。
4. **表格什么都不用做**：`DataTable` / `ClientTable` 各自读 `useTableDensity()`
   （紧凑 → antd `size="small"`，宽松 → `"middle"`）。页面不传 `density`、不落盘，
   `components/examples/table-example.tsx` 就是原样的例子。

文案（`t.navigation.appearance`、`t.appearanceSettings`）由 `createEnterpriseLabelCatalog` 的
zh / en 两份提供，宿主不必自己写。提示通道用根布局已经挂好的 `<Toaster />`。

## 宿主镜像清单

子模块更新后（EasyFrame + EasyUI 两个指针都要升），对照 blank：

- `lib/shell-adapter.ts` — `ShellIdentity` 加 `tableDensity`；`/auth/session` 的返回值从
  `string | null` 改成 `ShellSession`；新增 `tableDensityOf()` 与 `saveTableDensity()`。
- `lib/identity-cache.ts` — 快照带上 `tableDensity`（读回时同样水合），**`CACHE_VERSION` +1**
  （模板：1 → 2），`sameIdentity` 的标量键加上它，`reconcileIdentity` 在 `/auth/session`
  落地之前沿用快照里的档位（否则选了「宽松」的人每次刷新都要先看一眼紧凑的表格再跳回去）。
- `components/use-shell-identity.ts`（或宿主同名钩子）— `session` 落地时一并把
  `tableDensity` 写进身份，`sessionSettled = true`（服务端的答案即权威）。
- `components/table-density.tsx` — 整份复制，改 `TOAST_ID` 前缀。
- 外壳的身份 provider — 在身份 context 里面套 `TableDensityProvider`；**每一个**自带身份的
  外壳都要（EasyLearning 的答题页框架 `TakingFrame` 是第二个）。
- `app/…/settings/appearance/page.tsx` — 新增，无门禁；设置导航加「外观」项，`allowed` 恒真。
- 测试 — 适配器（水合 + PATCH 形状）、快照（往返 + 对账）、provider（乐观 / 回滚 / 回写快照 /
  不碰浏览器存储）、e2e（设置页改档 → 列表页换档 → 刷新仍在）。

EasyUI 导出名：`TableDensityProvider`、`useTableDensity`、`TableDensity`、
`DEFAULT_TABLE_DENSITY`、`tableSizeOf`（`@easy-enterprise/ui/table`）；
`EnterpriseAppearanceSettingsSurface`、`EnterpriseAppearanceSettingsLabels`
（`@easy-enterprise/ui/enterprise`）。

## 与 EasyLearning 实现的差异

- EasyLearning 在 `components/common/antd-provider.tsx` 里把两档的竖向内边距各收一档
  （`cellPaddingBlockSM: 10` / `cellPaddingBlockMD: 14`），让带行内动作的行落在 48 / 56px。
  那是它自己的控件几何，模板不带这一条；宿主的列表有行内动作时可以照抄。
- 模板的 provider 叫 `BlankTableDensityProvider`，住在 `components/`（blank 没有
  `components/common/` 这一层）。
