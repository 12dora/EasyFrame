import { createEnterpriseLabelCatalog, type EnterpriseCatalogLocale } from "@easy-enterprise/ui/enterprise";
import { localAccountsLabels } from "./local-accounts-labels";

export type Locale = EnterpriseCatalogLocale;

export function localeOf(value: string): Locale {
  return value === "en" ? "en" : "zh-CN";
}

export function messages(locale: Locale) {
  const brand = locale === "en"
    ? { appName: "Enterprise Starter", appDescription: "Identity-ready enterprise application foundation", footerText: "Enterprise Starter" }
    : { appName: "企业应用框架", appDescription: "统一身份与企业应用基础设施", footerText: "企业应用框架" };
  const shared = createEnterpriseLabelCatalog(locale, brand);
  return {
    ...shared,
    // `navigation.general` 与 `generalSettings` 由共享文案目录提供，宿主只补自有条目。
    navigation: {
      ...shared.navigation,
      accounts: locale === "en" ? "Local accounts" : "本地账户",
    },
    localAccounts: localAccountsLabels(locale),
    /** Brand fallback for `resolveEnterpriseBrand`; the configured title wins when set. */
    brand: brand.appName,
    dashboardTitle: locale === "en" ? "Workbench" : "工作台",
    dashboardDescription: locale === "en"
      ? "This blank site contains only the shared enterprise application foundation. Add business modules from here."
      : "这是一个只包含企业应用基础能力的空白站。业务模块可从这里开始接入。",
    ready: locale === "en" ? "Foundation ready" : "框架已就绪",
    forcedPasswordNotice: locale === "en"
      ? "You must change the temporary password before continuing."
      : "继续使用前必须修改临时密码。",
    examples: exampleMessages[locale],
    readyDetail: locale === "en"
      ? "The topbar, navigation, login, security, permission integration and upstream health are all rendered from the shared package."
      : "顶栏、菜单、登录、安全、权限集成与上游监控均来自共享包。",
  };
}

/**
 * 示例列表页的文案(`components/examples/table-example.tsx`)。
 * 新宿主接入真实列表后,连同示例页一起删掉整块(以及上面的 `examples:` 一行)。
 *
 * 按语言分块而不是逐条三元:整块删起来干净,也不会把 `messages()` 的分支数推高。
 */
const exampleMessages = {
  "zh-CN": {
    navLabel: "示例:表格",
    title: "列表表格",
    subtitle: "共享的表格约定:表头搜索 / 筛选 / 排序,状态住在地址栏,分页走服务端。",
    notice: "模板示例。接入真实列表页后,删掉 app/[locale]/app/examples、components/examples 与本文案块。",
    columns: { name: "名称", status: "状态", owner: "负责人", updatedAt: "更新时间" },
    status: { active: "在用", paused: "暂停", archived: "已归档" },
    /** `DataTableLabels`:表头下拉、排序提示、空态与每页条数,一张表一份。 */
    table: { search: "搜索", reset: "重置", filter: "筛选", sortAsc: "升序排列", sortDesc: "降序排列", empty: "没有符合条件的数据", pageSize: "条/页" },
  },
  en: {
    navLabel: "Example: table",
    title: "List table",
    subtitle: "The shared table conventions: header search / filter / sort, state in the URL, server pagination.",
    notice: "Template sample. Delete app/[locale]/app/examples, components/examples and this messages block once the app has a real list page.",
    columns: { name: "Name", status: "Status", owner: "Owner", updatedAt: "Updated" },
    status: { active: "Active", paused: "Paused", archived: "Archived" },
    table: { search: "Search", reset: "Reset", filter: "Filter", sortAsc: "Sort ascending", sortDesc: "Sort descending", empty: "No matching rows", pageSize: "/ page" },
  },
} as const;
