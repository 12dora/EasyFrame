# 通用设置

通用设置替换原先的双语页脚设置：同一条记录保存每语言的应用名称、副标题、页脚 HTML，以及一枚可选标志。空名称/副标题/标志表示「使用宿主默认」（前端回落到 i18n 品牌与默认标志资源）；后端不为这三项编造默认值。页脚 HTML 的缺省值仍由宿主提供（blank 为 `企业应用 · © {year}` / `Enterprise App · © {year}`）。`{year}` 由前端页脚组件替换，后端不展开。

## 线合同

公开 `GET /api/v1/app-settings/general`（无需登录）与管理 `PUT /api/v1/app-settings/general`（权限 `settings.app_setting.update`）交换：

```json
{
  "titleZh": "",
  "titleEn": "",
  "subtitleZh": "",
  "subtitleEn": "",
  "footerHtmlZh": "",
  "footerHtmlEn": "",
  "logoDataUrl": null
}
```

| 字段 | 限制 |
|---|---|
| `titleZh` / `titleEn` | ≤ 80 字，纯文本；去首尾空白；含 `<` / `>` → 422 |
| `subtitleZh` / `subtitleEn` | ≤ 200 字，同上 |
| `footerHtmlZh` / `footerHtmlEn` | ≤ 20 000 字，经共享 `sanitize_footer_html` 清洗 |
| `logoDataUrl` | `data:image/(png\|jpeg\|webp);base64,...`；解码后 ≤ 128 KiB；魔数须与声明类型一致。`null` 或 `""` 清除标志（存 null）。其它 → 422 |

Python 模型（`enterprise_platform/schemas.py`）：`GeneralSettings`（`PlatformModel`，camelCase 别名）与 `GeneralSettingsUpdate`（`StrictPlatformModel`）。字段为 snake_case：`title_zh`、`title_en`、`subtitle_zh`、`subtitle_en`、`footer_html_zh`、`footer_html_en`、`logo_data_url`。

端口：`AppSettingsPort.get_general()` / `save_general(settings, *, actor_id)`。`PlatformPorts.app_settings` 为规范属性名（原 `PlatformPorts.footer` 已删除）。

路由组：规范开关为 `PlatformRouteGroups.app_settings`（默认 True）。`footer` 仍可作为兼容别名：显式传入时覆盖 `app_settings`。前缀为 `/app-settings`，同时覆盖 `general` 与页脚 shim。

### 兼容 shim

- `GET /api/v1/app-settings/footer` 从 general 投影 `{footerHtmlZh, footerHtmlEn}`。
- `PUT /api/v1/app-settings/footer` 只写回 general 的两个页脚字段，名称/副标题/标志保持不变。

## 存储

blank / EasyLearning / EasyCustoms：`platform_settings` 一行 `key="general"`，值为 snake_case JSON。读取时若 `general` 不存在但遗留 `footer` 行仍在，则把其两个 HTML 字段投影进 general（名称/副标题/标志为空）。写入只写 `general`，不删旧 `footer` 行。审计动作仍为 `settings.footer.update`。

EasyTrade 不走 `platform_settings`，使用自有 `app_settings` 表（见该宿主文档）；接入本合同时应增列 `title_zh` / `title_en` / `subtitle_zh` / `subtitle_en` / `logo_data_url`，并继续关闭共享路由组（`PlatformRouteGroups(app_settings=False)` 或 `footer=False`）直到切到共享 GET/PUT。

## 宿主接入清单

子模块更新后，镜像宿主除使用新的 `enterprise_platform` 外，需对照 blank_app：

- `blank_app/adapter_platform.py` — `BlankAppSettingsAdapter.get_general` / `save_general`（含 `footer` 行回落；写入 `key="general"`）
- `blank_app/adapters.py` — 再导出 `BlankAppSettingsAdapter`、`GeneralSettings`、`validate_logo_data_url`、`clean_plain_text`
- `blank_app/main.py` — `PlatformPorts(app_settings=...)`（原 `footer=` 已更名）

EasyTrade 非镜像路径：`app/api/v1/app_settings.py` + `app_settings` 表，不要复制 blank 的 `PlatformSetting` 适配器。

前端模板（`frontend/apps/blank`，学习站按此复制）：

- `lib/shell-adapter.ts` — `loadGeneralSettings()` → `GET /api/v1/app-settings/general`；类型 `ShellGeneralSettings`
- 设置页路由 `app/[locale]/app/settings/general`（原 `settings/footer` 已删除）
- 保存后事件：`window.dispatchEvent(new CustomEvent("enterprise-starter:general-updated", { detail: value }))`（原 `footer-updated` 已退役）
- 顶栏品牌槽：EasyUI `EnterpriseBrandSlot`；接线见 C2。blank 壳在品牌渲染处留有 `// EasyUI EnterpriseBrandSlot wiring: see docs/GENERAL_SETTINGS.md`

EasyUI 导出名（包 `@easy-enterprise/ui/enterprise`）：`EnterpriseGeneralSettingsSurface`、`EnterpriseGeneralSettingsValue`、`EnterpriseGeneralSettingsAdapter`、`EnterpriseGeneralSettingsLabels`、`EnterpriseBrandSlot`、`useEnterpriseGeneralSettings`、`primeEnterpriseGeneralSettings`、`resolveEnterpriseBrand`、`resolveEnterpriseFooterHtml`。`EnterpriseFooterSettingsSurface` 已删除。导航文案键为 `navigation.general`（通用 / General）。
