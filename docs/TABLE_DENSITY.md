# 视觉效果（按账号保存的表格行高与表单行距）

设置 → 外观的「视觉效果」包含两项独立偏好，均为紧凑 / 宽松，缺省紧凑：

| 界面 | API 字段 | 账号 JSON 键 | EasyUI Provider | 作用 |
| --- | --- | --- | --- | --- |
| 表格 | `tableDensity` | `table_density` | `TableDensityProvider` | antd 表格 `size`（`small` / `middle`） |
| 行距 | `rowSpacing` | `row_spacing` | `RowSpacingProvider` | 表单标签、纵向栈与区块之间的间距 |

偏好存在账号的 `platform_accounts.ui_preferences`，浏览器身份快照只是缓存。
`RowSpacingProvider` 是 `<html data-ui-density>` 的唯一写入方；表格 Provider 只管表格行高。
宿主全局样式需要引入 `@easy-enterprise/ui/theme.css`。

## API 契约

- `GET /api/v1/auth/session` 同时返回 `preferences.tableDensity` 与 `preferences.rowSpacing`。
  历史账号没有 `row_spacing` 时，行距独立回落到 `compact`，不继承表格档位。
- `PATCH /api/v1/auth/preferences` 只发送要改的字段，例如 `{ "rowSpacing": "comfortable" }`。
  两字段都可省略；省略或 `null` 不改原值，未知键与非法档位返回 422。
  成功返回与 GET 相同形状的 `AuthSession`。
- 两条路由只要求登录，不要求业务权限码。审计事件 `auth.preferences.update` 的 before / after
  包含两项偏好。补丁合并与写入必须在同一行锁事务内完成，防止并发请求互相覆盖。
- 已有 `ui_preferences` JSON 列即可保存新键，不需要增加列或迁移。

共享账户路由与宿主后端接入说明见 [PERMISSION_ONBOARDING.md](PERMISSION_ONBOARDING.md)。

## 前端接线

1. `lib/shell-adapter.ts`：`ShellIdentity` 与 `ShellSession` 同时携带两项偏好；
   `saveTableDensity()` / `saveRowSpacing()` 各自只 PATCH 一个字段。
   `/auth/me` 先让外壳可用，`/auth/session` 后台补齐偏好。
2. `lib/identity-cache.ts`：身份快照同时保存两项，在 session 落地之前沿用缓存；
   `sameIdentity` 比较两项值。保存成功只合并本次修改的字段，保留当前身份中的其他字段。
   缓存已清除、账号或会话代次变化时，迟到响应不能重建旧身份。
3. `components/table-density.tsx` 与 `components/row-spacing.tsx`：外壳身份层挂上两个 Provider。
   在途请求乐观更新，失败回滚并提示；完成后以身份快照为准，后续身份刷新仍能更新显示。
   同一项保存时禁用该行选择，另一项可独立保存。
4. `app/[locale]/app/settings/appearance/page.tsx` 使用 `EnterpriseAppearanceSettingsSurface`。
   外观无需业务权限，导航恒显示；同页全局「显示页脚」仍要求 `settings.app_setting.update`，
   见 [GENERAL_SETTINGS.md](GENERAL_SETTINGS.md)。

文案由 `createEnterpriseLabelCatalog` 的 zh / en 两份提供，错误提示使用根布局 `<Toaster />`。

## 宿主镜像清单

- 更新 EasyFrame 与 EasyUI 子模块，镜像后端偏好适配器的两键合并与事务锁。
- 镜像 shell adapter、身份缓存、后台 session 加载和两个 Provider；检查缓存版本与旧数据水合。
- 每个独立身份外壳都挂两个 Provider，包含 EasyLearning 的答题页 `TakingFrame`。
- 纵向布局按 EasyUI 的 `ui-stack-sm` / `ui-stack` / `ui-stack-lg` 尺度接入；
  横向间距、内边距、控件高度、表格内部与文字簇间距不随这次清扫改变。
- 验证两字段默认值与单字段 PATCH、两项并发保存、同项重复点击、失败回滚、身份刷新，
  以及退出登录或切换账号后的迟到响应。后端并发测试必须使用隔离 PostgreSQL 库。

EasyUI 的表格导出来自 `@easy-enterprise/ui/table`；行距导出
`RowSpacingProvider`、`useRowSpacing`、`RowSpacing` 来自 `@easy-enterprise/ui`。

EasyLearning 额外在 `components/common/antd-provider.tsx` 将表格竖向内边距设为
`cellPaddingBlockSM: 10` / `cellPaddingBlockMD: 14`，使带行内动作的表格行为 48 / 56px；
这是宿主控件尺寸，不属于表单行距。
