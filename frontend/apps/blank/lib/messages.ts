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
    navigation: {
      ...shared.navigation,
      accounts: locale === "en" ? "Local accounts" : "本地账户",
    },
    localAccounts: localAccountsLabels(locale),
    brand: brand.appName,
    subtitle: brand.appDescription,
    dashboardTitle: locale === "en" ? "Workbench" : "工作台",
    dashboardDescription: locale === "en"
      ? "This blank site contains only the shared enterprise application foundation. Add business modules from here."
      : "这是一个只包含企业应用基础能力的空白站。业务模块可从这里开始接入。",
    ready: locale === "en" ? "Foundation ready" : "框架已就绪",
    forcedPasswordNotice: locale === "en"
      ? "You must change the temporary password before continuing."
      : "继续使用前必须修改临时密码。",
    readyDetail: locale === "en"
      ? "The topbar, navigation, login, security, permission integration and upstream health are all rendered from the shared package."
      : "顶栏、菜单、登录、安全、权限集成与上游监控均来自共享包。",
  };
}
