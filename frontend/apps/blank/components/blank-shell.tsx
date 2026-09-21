"use client";

import { MobileNav, Sidebar, Topbar, useNavIntent, type NavModel, type NavPanel, type RenderNavLink } from "@easy-enterprise/ui/shell";
import { PageLoadingSkeleton } from "@easy-enterprise/ui";
import { EnterpriseAppFrame, EnterpriseBrandSlot, EnterpriseConfiguredFooter, EnterprisePermissionOnboarding, EnterprisePublicShell, EnterpriseTopbarActions, hasEnterpriseBusinessAccess, performEnterpriseLogout, resolveEnterpriseBrand, resolveEnterpriseFooterHtml, resolveEnterpriseShowFooter, useEnterpriseGeneralSettings, type EnterpriseGeneralSettingsValue } from "@easy-enterprise/ui/enterprise";
import { MotionConfig } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode, type SyntheticEvent } from "react";
import brandLogo from "../assets/brand/jiefa_logo.webp";
import { BlankAntdProvider } from "./antd-provider";
import { enterpriseLogoutAdapter } from "../lib/auth-adapter";
import { localeOf, messages, type Locale } from "../lib/messages";
import { BLANK_BUSINESS_PERMISSION_CODES } from "../lib/permissions";
import { loadGeneralSettings, type ShellIdentity } from "../lib/shell-adapter";
import { BlankRowSpacingProvider } from "./row-spacing";
import { BlankTableDensityProvider } from "./table-density";
import { onboardingReady, useShellIdentity } from "./use-shell-identity";
import { useShellNotifications } from "./use-shell-notifications";

export const SETTINGS_PANEL_TEST_ID = "blank-nav-settings";

const BlankShellIdentityContext = createContext<ShellIdentity | null>(null);
export function useBlankShellIdentity() { const identity = useContext(BlankShellIdentityContext); if (!identity) throw new Error("Blank shell identity is not available"); return identity; }

/**
 * 身份 context + 跟着身份走的偏好 provider。
 *
 * 表格行高(`identity.tableDensity`)与行距(`identity.rowSpacing`)都是账号偏好,所以两个
 * provider 就该挂在「身份可用」的那一层:外壳每包一次身份,它们也就只接一次,设置页、
 * 所有列表页与所有表单读的是同一份档位。两份偏好互不相干,各自套一层。
 */
function BlankShellIdentityProvider({ value, children }: { value: ShellIdentity; children: ReactNode }) {
  return (
    <BlankShellIdentityContext.Provider value={value}>
      <BlankTableDensityProvider identity={value}>
        <BlankRowSpacingProvider identity={value}>{children}</BlankRowSpacingProvider>
      </BlankTableDensityProvider>
    </BlankShellIdentityContext.Provider>
  );
}

/**
 * 导航意图的通道:`renderNavLink` 是模块级常量(传给侧栏的 prop 身份保持稳定),
 * 捞不到外壳闭包里的 `nav.onIntent`,所以走一层 context 传下去。
 */
const NavIntentContext = createContext<(href: string) => void>(() => undefined);

/**
 * 侧栏链接:不随进入视口预取(一屏多个入口会在首屏同时打出多次 RSC 请求),
 * 指针悬停 / 键盘聚焦 / 触摸时才预取 —— 真要点的那一个在点下去之前就开始加载了。
 *
 * 普通左键点击时先同步记下导航意图(`onIntent`),侧栏的选中标记因此在路由提交之前就挪过去;
 * 这一步必须排在 `onNavigate()`(移动端抽屉关闭等外壳副作用)之前。
 *
 * 只认「普通左键点击」:带修饰键(⌘/Ctrl/Shift/Alt)、中键、以及已被 `preventDefault()` 的点击
 * 都不会真的导航(Next 的 `Link` 仍会调用宿主的 onClick),记了意图 `pathname` 永远不变,
 * 标记就会在错误的条目上停满 `NAV_INTENT_TIMEOUT_MS`。
 */
function NavLinkWithIntentPrefetch({ href, active, className, testId, onNavigate, children }: Parameters<RenderNavLink>[0]) {
  const router = useRouter();
  const onIntent = useContext(NavIntentContext);
  const prefetch = () => router.prefetch(href);
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    const plain = event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !event.defaultPrevented;
    if (plain) onIntent(href);
    onNavigate();
  };
  return <Link href={href} prefetch={false} aria-current={active ? "page" : undefined} className={className} data-test-id={testId} onClick={onClick} onMouseEnter={prefetch} onFocus={prefetch} onTouchStart={prefetch}>{children}</Link>;
}

const renderNavLink: RenderNavLink = (props) => <NavLinkWithIntentPrefetch {...props}/>;

/**
 * 设置入口是 EasyUI 侧栏里的按钮(不是链接),宿主改不了它的 DOM:在外层用事件委托,
 * 指针移到 / 聚焦到入口上时预取它要跳去的第一页。`display: contents` 不影响侧栏布局。
 */
function SettingsEntryPrefetch({ firstHref, children }: { firstHref: string | null; children: ReactNode }) {
  const router = useRouter();
  const prefetchIfSettingsEntry = (event: SyntheticEvent) => {
    if (!firstHref || !(event.target instanceof Element)) return;
    if (event.target.closest(`[data-test-id="${SETTINGS_PANEL_TEST_ID}"]`)) router.prefetch(firstHref);
  };
  return <div className="contents" onPointerOver={prefetchIfSettingsEntry} onFocus={prefetchIfSettingsEntry}>{children}</div>;
}

type ShellTopbarProps = { locale: Locale; pathname: string; t: ReturnType<typeof messages>; identity: ShellIdentity; brand: ReturnType<typeof resolveEnterpriseBrand>; canSecurity: boolean; canNotifications: boolean; leading?: ReactNode };

/** 顶栏 + 通知状态:通知数据的每次变化只重画这一层,不牵动外壳与页面。 */
function ShellTopbar({ locale, pathname, t, identity, brand, canSecurity, canNotifications, leading }: ShellTopbarProps) {
  const router = useRouter(); const href = (path: string) => `/${locale}${path}`;
  const notifications = useShellNotifications(canNotifications, href("/app/notifications"), identity.accountId);
  return <Topbar leading={leading} brand={
    <EnterpriseBrandSlot href={href("/app")} title={brand.title} subtitle={brand.subtitle} logoSrc={brand.logoSrc} testId="app-brand" renderLink={({ href: target, className, children: label, testId }) => <Link href={target} className={className} data-test-id={testId}>{label}</Link>}/>
  } actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={t.shell} notifications={notifications} user={{ name: identity.name, identity: identity.identity, avatarUrl: identity.avatarUrl, permissionSummary: t.common.permissionCount(identity.permissions.size) }} securityHref={canSecurity ? href("/app/settings/security") : undefined} renderLink={({ href: target, className, testId, role, children: label }) => <Link href={target} className={className} data-test-id={testId} role={role}>{label}</Link>} onLogout={() => performEnterpriseLogout(enterpriseLogoutAdapter, () => router.replace(`/${locale}/logged-out`))}/>} />;
}

/**
 * 外壳上跟通用设置走的那几样:顶栏品牌、框架页脚、抽屉页脚,以及全局「显示页脚」开关(设置 → 外观)。
 *
 * 通用设置由共享缓存供给:顶栏、页脚与设置页共用一次公开 GET,保存后立即重刷。共享读取落地之前先用
 * 外壳段 `layout.tsx` 在 SSR 交过来的那份(只作渲染期回退、不灌进缓存,客户端 GET 照发且以它为准):
 * 全局关掉页脚的站点首帧就不画页脚,品牌也不会先闪一下默认值。
 *
 * 页脚要传两遍:框架的页脚包裹层在手机上是 `hidden md:block`,手机那一份由导航抽屉底部承载。
 * 抽屉里用 `bare`(抽屉本身是 role="dialog",再嵌一个 <footer> 会多出一个 contentinfo 地标)。
 * 关掉页脚时框架用 `showFooter` 收起(`footer` 仍必填,不靠不传);抽屉那份干脆不给 ——
 * `MobileNav` 拿不到 `footer` 就连那条 `border-t` 区域都不画。
 */
function useShellGeneralSettings(locale: Locale, t: ReturnType<typeof messages>, initialGeneralSettings: EnterpriseGeneralSettingsValue | null | undefined) {
  const { settings: cached } = useEnterpriseGeneralSettings(loadGeneralSettings);
  const settings = cached ?? initialGeneralSettings ?? null;
  const brand = resolveEnterpriseBrand(settings, locale, { title: t.brand, subtitle: null, logoSrc: brandLogo.src });
  const footerHtml = resolveEnterpriseFooterHtml(settings, locale);
  const showFooter = resolveEnterpriseShowFooter(settings);
  const footerFallback = <>{t.public.footer} · © {new Date().getFullYear()}</>;
  return {
    brand,
    showFooter,
    footer: <EnterpriseConfiguredFooter html={footerHtml} fallback={footerFallback}/>,
    drawerFooter: showFooter ? <EnterpriseConfiguredFooter bare html={footerHtml} fallback={footerFallback}/> : undefined,
  };
}

export interface BlankShellProps {
  children: ReactNode;
  locale: string;
  /** 外壳段 `layout.tsx` 在 SSR 取到的通用设置;取不到是 `null`,退回客户端自己读。 */
  initialGeneralSettings?: EnterpriseGeneralSettingsValue | null;
}

export function BlankShell({ children, locale: rawLocale, initialGeneralSettings }: BlankShellProps) {
  const locale = localeOf(rawLocale); const t = useMemo(() => messages(locale), [locale]); const pathname = usePathname(); const router = useRouter();
  // 导航意图:`usePathname()` 要等 RSC 提交才翻页,远端点侧栏会「不跟手」。`nav.path` 在导航
  // 落地前就是刚点的那一项,只用来算选中态 / 面板开合;真实 `pathname` 仍归 pathKey、身份
  // 复查、语言切换等一切会发请求的地方(见 docs/SHELL_NAV_INTENT.md)。
  const nav = useNavIntent(pathname);
  const inSettings = nav.path.includes("/settings/");
  const [panel, setPanel] = useState<string | null>(inSettings ? "settings" : null);
  const wasInSettings = useRef(inSettings);
  // 生产身份事实来自可信网关/Authentik 注入后由 `/auth/me` 验证；本地 JWT 只是
  // 开发/demo 的可选凭据，不能成为进入框架站的前置门禁。`/auth/session` 只补申请入口。
  const identityLabels = useMemo(() => ({ ...t.identity, separator: t.access.authorization.roleGroupSeparator }), [t]);
  // 身份、强制改密拦截、401 踢人与跨标签页换人都在这个钩子里（感知加载：本标签页快照秒开、
  // `/auth/session` 后台补齐、导航后的例行复查排到空闲）。
  const { identity, permissionUrlPending, refreshIdentity } = useShellIdentity({ locale, pathname, fallbackName: t.brand, identityLabels });
  useEffect(() => { if (inSettings && !wasInSettings.current) setPanel("settings"); if (!inSettings) setPanel(null); wasInSettings.current = inSettings; }, [inSettings]);
  const canNotifications = Boolean(identity?.permissions.has("notification.center.view"));
  const { brand, footer, drawerFooter, showFooter } = useShellGeneralSettings(locale, t, initialGeneralSettings);
  const href = useCallback((path: string) => `/${locale}${path}`, [locale]); const active = useCallback((path: string) => nav.path === href(path), [href, nav.path]); const activePrefix = useCallback((path: string) => nav.path === href(path) || nav.path.startsWith(`${href(path)}/`), [href, nav.path]);
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
      // 「外观」只改当前账号自己的偏好，没有权限门禁：任何登录用户都进得去。
      { key: "appearance", label: t.navigation.appearance, href: href("/app/settings/appearance"), active: activePrefix("/app/settings/appearance") },
      // 「通知」同样没有门禁:后端按 gate 权限过滤分组,一个分组都没有的账号看到的是空状态,
      // 不是 403。与收件箱(顶栏通知中心)是两件事,别用 `notification.center.view` 去卡它。
      { key: "notifications", label: t.navigation.notificationSettings, href: href("/app/settings/notifications"), active: activePrefix("/app/settings/notifications") },
      ...(canSecurity ? [{ key: "security", label: t.navigation.security, href: href("/app/settings/security"), active: activePrefix("/app/settings/security") }] : []),
      ...(canAccess ? [{ key: "access", label: t.navigation.access, href: href("/app/settings/access"), active: activePrefix("/app/settings/access") }] : []),
      ...(canAccounts ? [{ key: "accounts", label: t.navigation.accounts, href: href("/app/settings/accounts"), active: activePrefix("/app/settings/accounts") }] : []),
      ...(canUpstream ? [{ key: "upstream", label: t.navigation.upstream, href: href("/app/settings/upstream"), active: activePrefix("/app/settings/upstream") }] : []),
    ];
    // 示例分组:新宿主复制模板后,连同 `app/[locale]/app/examples` 与 `components/examples` 一起删掉。
    const examplesGroup = { key: "examples", divider: true, nodes: [{ kind: "link" as const, link: { key: "examples-table", testId: "blank-nav-examples-table", label: t.examples.navLabel, href: href("/app/examples/table"), active: activePrefix("/app/examples") } }] };
    return { groups: [{ key: "main", nodes: [{ kind: "link", link: { key: "dashboard", testId: "blank-nav-dashboard", label: t.navigation.dashboard, href: href("/app"), active: active("/app") } }] }, examplesGroup, ...(settingsItems.length ? [{ key: "system", divider: true, nodes: [{ kind: "panel" as const, panel: { id: "settings", testId: SETTINGS_PANEL_TEST_ID, label: t.navigation.settings, active: inSettings, firstHref: settingsItems[0].href, items: settingsItems } }] }] : [])] };
  }, [active, activePrefix, canSecurity, href, identity, inSettings, t]);
  // 面板入口也是一次导航:先记意图,标记与面板立刻就位,再交给 router。
  const openPanel = (next: NavPanel) => { nav.onIntent(next.firstHref); setPanel(next.id); router.push(next.firstHref); };
  const loading = <main className="mx-auto w-full max-w-6xl p-6" data-test-id="blank-auth-loading"><PageLoadingSkeleton/></main>;
  if (!identity) return loading;
  // 强制改密壳没有导航模型可进(那一页只能改密码),顶栏不带 `leading`;
  // 应用框架那一份在下面补上手机汉堡。
  const topbarProps = { locale, pathname, t, identity, brand, canSecurity, canNotifications };
  const topbar = <ShellTopbar {...topbarProps}/>;
  const forcedTarget = `/${locale}/app/settings/security/password`;
  // antd 环境只包一层,受保护内容与强制改密页共用同一个 provider。
  const content = <BlankAntdProvider locale={locale}>{children}</BlankAntdProvider>;
  if (identity.mustChangePassword && pathname === forcedTarget) return <BlankShellIdentityProvider value={identity}><EnterprisePublicShell topbar={topbar} footer={footer} showFooter={showFooter}>{content}</EnterprisePublicShell></BlankShellIdentityProvider>;
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
  const openPanelId = inSettings ? panel : null;
  // 手机上只留一条头部栏:汉堡按钮进 `Topbar` 的 `leading` 槽(`variant="trigger"`),
  // 框架不再收 `mobileNav`(分区栏整条去掉,正文紧跟顶栏开始);页脚见 docs/SHELL_MOBILE.md。
  const mobileNav = <MobileNav variant="trigger" model={model} renderLink={renderNavLink} backLabel={t.navigation.backToMain} menuLabel={t.navigation.menu} closeLabel={t.navigation.close} navLabel={t.navigation.menu} pathKey={pathname} footer={drawerFooter}/>;
  return <BlankShellIdentityProvider value={identity}><NavIntentContext.Provider value={nav.onIntent}><MotionConfig reducedMotion="user"><EnterpriseAppFrame pending={nav.pending} pendingLabel={t.common.loading} topbar={<ShellTopbar {...topbarProps} leading={mobileNav}/>} sidebar={<SettingsEntryPrefetch firstHref={settingsFirstHref(model)}><Sidebar model={model} openPanelId={openPanelId} onOpenPanel={openPanel} onBack={() => setPanel(null)} renderLink={renderNavLink} backLabel={t.navigation.backToMain} navLabel={t.navigation.menu}/></SettingsEntryPrefetch>} footer={footer} showFooter={showFooter} mainClassName="pb-6 md:pb-12">{content}</EnterpriseAppFrame></MotionConfig></NavIntentContext.Provider></BlankShellIdentityProvider>;
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
