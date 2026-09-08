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
      general: locale === "en" ? "General" : "通用",
    },
    generalSettings: locale === "en"
      ? {
          title: "General",
          description: "Set the application title, subtitle, logo, and footer.",
          loading: "Loading",
          loadFailed: "Could not load settings",
          retry: "Retry",
          localeTabs: { "zh-CN": "中文", en: "English" },
          appTitle: "Application title",
          appTitleHint: "Shown in the top bar. Leave blank to use the default.",
          subtitle: "Subtitle",
          subtitleHint: "Shown next to the title. Leave blank to use the default.",
          footerHtml: "Footer",
          footerHtmlHint: "Limited formatting is allowed.",
          logo: "Logo",
          logoHint: "PNG, JPEG, or WebP. At most 128 KB.",
          logoUpload: "Upload",
          logoRemove: "Remove",
          logoInvalid: "This image type is not supported.",
          logoTooLarge: "The image is too large.",
          save: "Save",
          saving: "Saving",
          saved: "Saved",
          saveFailed: "Could not save",
        }
      : {
          title: "通用",
          description: "设置应用名称、副标题、标志与页脚。",
          loading: "加载中",
          loadFailed: "无法加载设置",
          retry: "重试",
          localeTabs: { "zh-CN": "中文", en: "English" },
          appTitle: "应用名称",
          appTitleHint: "显示在顶栏。留空则使用默认名称。",
          subtitle: "副标题",
          subtitleHint: "显示在名称旁。留空则使用默认副标题。",
          footerHtml: "页脚",
          footerHtmlHint: "仅支持有限格式。",
          logo: "标志",
          logoHint: "支持 PNG、JPEG 或 WebP，不超过 128 KB。",
          logoUpload: "上传",
          logoRemove: "移除",
          logoInvalid: "不支持该图片类型。",
          logoTooLarge: "图片过大。",
          save: "保存",
          saving: "保存中",
          saved: "已保存",
          saveFailed: "保存失败",
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
