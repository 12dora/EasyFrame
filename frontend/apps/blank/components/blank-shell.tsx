"use client";

import { MobileNav, Sidebar, Topbar, type NavModel, type NavPanel, type RenderNavLink } from "@easy-enterprise/ui/shell";
import { PageLoadingSkeleton } from "@easy-enterprise/ui";
import { EnterpriseAppFrame, EnterpriseBrandSlot, EnterpriseConfiguredFooter, EnterprisePermissionOnboarding, EnterprisePublicShell, EnterpriseTopbarActions, hasEnterpriseBusinessAccess, performEnterpriseLogout, resolveEnterpriseBrand, resolveEnterpriseFooterHtml, useEnterpriseGeneralSettings } from "@easy-enterprise/ui/enterprise";
import { MotionConfig } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import brandLogo from "../assets/brand/jiefa_logo.webp";
import { BlankAntdProvider } from "./antd-provider";
import { enterpriseLogoutAdapter } from "../lib/auth-adapter";
import { localeOf, messages, type Locale } from "../lib/messages";
import { BLANK_BUSINESS_PERMISSION_CODES } from "../lib/permissions";
import { loadGeneralSettings, type ShellIdentity } from "../lib/shell-adapter";
import { onboardingReady, useShellIdentity } from "./use-shell-identity";
import { useShellNotifications } from "./use-shell-notifications";

export const SETTINGS_PANEL_TEST_ID = "blank-nav-settings";

const BlankShellIdentityContext = createContext<ShellIdentity | null>(null);
export function useBlankShellIdentity() { const identity = useContext(BlankShellIdentityContext); if (!identity) throw new Error("Blank shell identity is not available"); return identity; }

/**
 * 侧栏链接:不随进入视口预取(一屏多个入口会在首屏同时打出多次 RSC 请求),
 * 指针悬停 / 键盘聚焦 / 触摸时才预取 —— 真要点的那一个在点下去之前就开始加载了。
 */
function NavLinkWithIntentPrefetch({ href, active, className, testId, onNavigate, children }: Parameters<RenderNavLink>[0]) {
  const router = useRouter();
  const prefetch = () => router.prefetch(href);
  return <Link href={href} prefetch={false} aria-current={active ? "page" : undefined} className={className} data-test-id={testId} onClick={onNavigate} onMouseEnter={prefetch} onFocus={prefetch} onTouchStart={prefetch}>{children}</Link>;
}

const renderNavLink: RenderNavLink = (props) => <NavLinkWithIntentPrefetch {...props}/>;

/**
 * 设置入口是 EasyUI 侧栏里的按钮(不是链接),宿主改不了它的 DOM:在外层用事件委托,
 * 指针移到 / 聚焦到入口上时预取它要跳去的第一页。`display: contents` 不影响侧栏布局。
 */
function SettingsEntryPrefetch({ firstHref, children }: { firstHref: string | null; children: ReactNode }) {
  const router = useRouter();
  const onIntent = (event: SyntheticEvent) => {
    if (!firstHref || !(event.target instanceof Element)) return;
    if (event.target.closest(`[data-test-id="${SETTINGS_PANEL_TEST_ID}"]`)) router.prefetch(firstHref);
  };
  return <div className="contents" onPointerOver={onIntent} onFocus={onIntent}>{children}</div>;
}

type ShellTopbarProps = { locale: Locale; pathname: string; t: ReturnType<typeof messages>; identity: ShellIdentity; brand: ReturnType<typeof resolveEnterpriseBrand>; canSecurity: boolean; canNotifications: boolean };

/** 顶栏 + 通知状态:通知数据的每次变化只重画这一层,不牵动外壳与页面。 */
function ShellTopbar({ locale, pathname, t, identity, brand, canSecurity, canNotifications }: ShellTopbarProps) {
  const router = useRouter(); const href = (path: string) => `/${locale}${path}`;
  const notifications = useShellNotifications(canNotifications, href("/app/notifications"), identity.accountId);
  return <Topbar brand={
    <EnterpriseBrandSlot href={href("/app")} title={brand.title} subtitle={brand.subtitle} logoSrc={brand.logoSrc} testId="app-brand" renderLink={({ href: target, className, children: label, testId }) => <Link href={target} className={className} data-test-id={testId}>{label}</Link>}/>
  } actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={t.shell} notifications={notifications} user={{ name: identity.name, identity: identity.identity, avatarUrl: identity.avatarUrl, permissionSummary: t.common.permissionCount(identity.permissions.size) }} securityHref={canSecurity ? href("/app/settings/security") : undefined} renderLink={({ href: target, className, testId, role, children: label }) => <Link href={target} className={className} data-test-id={testId} role={role}>{label}</Link>} onLogout={() => performEnterpriseLogout(enterpriseLogoutAdapter, () => router.replace(`/${locale}/logged-out`))}/>} />;
}

export function BlankShell({ children, locale: rawLocale }: { children: ReactNode; locale: string }) {
  const locale = localeOf(rawLocale); const t = useMemo(() => messages(locale), [locale]); const pathname = usePathname(); const router = useRouter();
  const [panel, setPanel] = useState<string | null>(pathname.includes("/settings/") ? "settings" : null);
  const wasInSettings = useRef(pathname.includes("/settings/"));
  // 生产身份事实来自可信网关/Authentik 注入后由 `/auth/me` 验证；本地 JWT 只是
  // 开发/demo 的可选凭据，不能成为进入框架站的前置门禁。`/auth/session` 只补申请入口。
  const identityLabels = useMemo(() => ({ ...t.identity, separator: t.access.authorization.roleGroupSeparator }), [t]);
  // 身份、强制改密拦截、401 踢人与跨标签页换人都在这个钩子里（感知加载：本标签页快照秒开、
  // `/auth/session` 后台补齐、导航后的例行复查排到空闲）。
  const { identity, permissionUrlPending, refreshIdentity } = useShellIdentity({ locale, pathname, fallbackName: t.brand, identityLabels });
  useEffect(() => { const inSettings = pathname.includes("/settings/"); if (inSettings && !wasInSettings.current) setPanel("settings"); if (!inSettings) setPanel(null); wasInSettings.current = inSettings; }, [pathname]);
  const canNotifications = Boolean(identity?.permissions.has("notification.center.view"));
  // 通用设置由共享缓存供给：顶栏、页脚与设置页共用一次公开 GET，保存后立即重刷。
  const { settings } = useEnterpriseGeneralSettings(loadGeneralSettings);
  const brand = resolveEnterpriseBrand(settings, locale, { title: t.brand, subtitle: null, logoSrc: brandLogo.src });
  const footer = <EnterpriseConfiguredFooter html={resolveEnterpriseFooterHtml(settings, locale)} fallback={<>{t.public.footer} · © {new Date().getFullYear()}</>}/>;
  const href = useCallback((path: string) => `/${locale}${path}`, [locale]); const active = useCallback((path: string) => pathname === href(path), [href, pathname]); const activePrefix = useCallback((path: string) => pathname === href(path) || pathname.startsWith(`${href(path)}/`), [href, pathname]);
  const canSecurity = Boolean(identity && Object.values(identity.securityCapabilities).some(Boolean));
  const model: NavModel = useMemo(() => {
    const permissions = identity?.permissions ?? new Set<string>();
    const canAccess = permissions.has("identity.integration.view") || permissions.has("authz.integration.view");
    const canAccounts = permissions.has("accounts.local.view");
    const canUpstream = permissions.has("ops.upstream_health.view");
    const canGeneral = permissions.has("settings.app_setting.update");
    // 「通用」是应用级设置，排在账号与运维项之前。
    const settingsItems = [
      ...(canGeneral ? [{ key: "general", label: t.navigation.general, href: href("/app/settings/general"), active: activePrefix("/app/settings/general") }] : []),
      ...(canSecurity ? [{ key: "security", label: t.navigation.security, href: href("/app/settings/security"), active: activePrefix("/app/settings/security") }] : []),
      ...(canAccess ? [{ key: "access", label: t.navigation.access, href: href("/app/settings/access"), active: activePrefix("/app/settings/access") }] : []),
      ...(canAccounts ? [{ key: "accounts", label: t.navigation.accounts, href: href("/app/settings/accounts"), active: activePrefix("/app/settings/accounts") }] : []),
      ...(canUpstream ? [{ key: "upstream", label: t.navigation.upstream, href: href("/app/settings/upstream"), active: activePrefix("/app/settings/upstream") }] : []),
    ];
    // 示例分组:新宿主复制模板后,连同 `app/[locale]/app/examples` 与 `components/examples` 一起删掉。
    const examplesGroup = { key: "examples", divider: true, nodes: [{ kind: "link" as const, link: { key: "examples-table", testId: "blank-nav-examples-table", label: t.examples.navLabel, href: href("/app/examples/table"), active: activePrefix("/app/examples") } }] };
    return { groups: [{ key: "main", nodes: [{ kind: "link", link: { key: "dashboard", testId: "blank-nav-dashboard", label: t.navigation.dashboard, href: href("/app"), active: active("/app") } }] }, examplesGroup, ...(settingsItems.length ? [{ key: "system", divider: true, nodes: [{ kind: "panel" as const, panel: { id: "settings", testId: SETTINGS_PANEL_TEST_ID, label: t.navigation.settings, active: pathname.includes("/settings/"), firstHref: settingsItems[0].href, items: settingsItems } }] }] : [])] };
  }, [active, activePrefix, canSecurity, href, identity, pathname, t]);
  const openPanel = (next: NavPanel) => { setPanel(next.id); router.push(next.firstHref); };
  const loading = <main className="mx-auto w-full max-w-6xl p-6" data-test-id="blank-auth-loading"><PageLoadingSkeleton/></main>;
  if (!identity) return loading;
  const topbar = <ShellTopbar locale={locale} pathname={pathname} t={t} identity={identity} brand={brand} canSecurity={canSecurity} canNotifications={canNotifications}/>;
  const forcedTarget = `/${locale}/app/settings/security/password`;
  // antd 环境只包一层,受保护内容与强制改密页共用同一个 provider。
  const content = <BlankAntdProvider locale={locale}>{children}</BlankAntdProvider>;
  if (identity.mustChangePassword && pathname === forcedTarget) return <BlankShellIdentityContext.Provider value={identity}><EnterprisePublicShell topbar={topbar} footer={footer}>{content}</EnterprisePublicShell></BlankShellIdentityContext.Provider>;
  if (!hasEnterpriseBusinessAccess({ permissions: identity.permissions, securityCapabilities: identity.securityCapabilities, isLocalSuperadmin: identity.isLocalSuperadmin, businessPermissionCodes: BLANK_BUSINESS_PERMISSION_CODES })) {
    // 引导页的全部内容就是那个申请入口，没它不成页 —— 只有这一页会等 `/auth/session`。
    if (!onboardingReady(identity, permissionUrlPending)) return loading;
    const secondary = identity.email && identity.email !== identity.name ? identity.email : undefined;
    return (
      <EnterprisePermissionOnboarding
        identity={{ displayName: identity.name, secondaryLabel: secondary, avatarUrl: identity.avatarUrl }}
        permissionRequestUrl={identity.permissionRequestUrl}
        onRecheck={refreshIdentity}
        onLogout={() => { void performEnterpriseLogout(enterpriseLogoutAdapter, () => router.replace(`/${locale}/logged-out`)); }}
        labels={t.permissionOnboarding}
      />
    );
  }
  const openPanelId = pathname.includes("/settings/") ? panel : null;
  return <BlankShellIdentityContext.Provider value={identity}><MotionConfig reducedMotion="user"><EnterpriseAppFrame topbar={topbar} sidebar={<SettingsEntryPrefetch firstHref={settingsFirstHref(model)}><Sidebar model={model} openPanelId={openPanelId} onOpenPanel={openPanel} onBack={() => setPanel(null)} renderLink={renderNavLink} backLabel={t.navigation.backToMain} navLabel={t.navigation.menu}/></SettingsEntryPrefetch>} mobileNav={<MobileNav model={model} renderLink={renderNavLink} backLabel={t.navigation.backToMain} menuLabel={t.navigation.menu} closeLabel={t.navigation.close} navLabel={t.navigation.menu} pathKey={pathname}/>} footer={footer} mainClassName="pb-12">{content}</EnterpriseAppFrame></MotionConfig></BlankShellIdentityContext.Provider>;
}

function localizedLocation(pathname: string, locale: string, nextLocale: string) {
  const query = typeof window === "undefined" ? "" : window.location.search + window.location.hash;
  return `${pathname.replace(`/${locale}`, `/${nextLocale}`)}${query}`;
}

/** 导航模型里设置面板要跳去的第一页;没有设置入口时为 null。 */
function settingsFirstHref(model: NavModel): string | null {
  for (const group of model.groups) for (const node of group.nodes) if (node.kind === "panel" && node.panel.id === "settings") return node.panel.firstHref;
  return null;
}
