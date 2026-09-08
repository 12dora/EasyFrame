"use client";

import { MobileNav, Sidebar, Topbar, type NavModel, type NavPanel, type RenderNavLink } from "@easy-enterprise/ui/shell";
import { PageLoadingSkeleton } from "@easy-enterprise/ui";
import { EnterpriseAppFrame, EnterpriseBrandSlot, EnterpriseConfiguredFooter, EnterprisePublicShell, EnterpriseTopbarActions, performEnterpriseLogout, resolveEnterpriseBrand, resolveEnterpriseFooterHtml, useEnterpriseGeneralSettings, type EnterpriseNotification } from "@easy-enterprise/ui/enterprise";
import { MotionConfig } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import brandLogo from "../assets/brand/jiefa_logo.webp";
import { enterpriseLogoutAdapter, logout } from "../lib/auth-adapter";
import { localeOf, messages } from "../lib/messages";
import { BLANK_AUTH_INVALIDATED_EVENT } from "../lib/platform-api";
import { dismissAllNotifications, dismissNotification, loadGeneralSettings, loadNotifications, loadShellIdentity, type ShellIdentity } from "../lib/shell-adapter";

const BlankShellIdentityContext = createContext<ShellIdentity | null>(null);
export function useBlankShellIdentity() { const identity = useContext(BlankShellIdentityContext); if (!identity) throw new Error("Blank shell identity is not available"); return identity; }

export function BlankShell({ children, locale: rawLocale }: { children: ReactNode; locale: string }) {
  const locale = localeOf(rawLocale); const t = useMemo(() => messages(locale), [locale]); const pathname = usePathname(); const router = useRouter();
  const [panel, setPanel] = useState<string | null>(pathname.includes("/settings/") ? "settings" : null); const [identity, setIdentity] = useState<ShellIdentity | null>(null);
  const wasInSettings = useRef(pathname.includes("/settings/"));
  const [notifications, setNotifications] = useState<EnterpriseNotification[]>([]); const [notificationsLoading, setNotificationsLoading] = useState(false); const [notificationsError, setNotificationsError] = useState(false);
  const refreshNotifications = useCallback(async () => { setNotificationsLoading(true); setNotificationsError(false); try { setNotifications(await loadNotifications()); } catch { setNotificationsError(true); } finally { setNotificationsLoading(false); } }, []);
  // 生产身份事实来自可信网关/Authentik 注入后由 `/auth/me` 验证；本地 JWT 只是
  // 开发/demo 的可选凭据，不能成为进入框架站的前置门禁。
  useEffect(() => { let alive = true; const forcedTarget = `/${locale}/app/settings/security/password`; loadShellIdentity(t.brand, { ...t.identity, separator: t.access.authorization.roleGroupSeparator }).then((value) => { if (!alive) return; if (value.mustChangePassword && pathname !== forcedTarget) { router.replace(forcedTarget); return; } setIdentity(value); }).catch(() => { if (alive) { logout(); router.replace(`/${locale}/login?next=${encodeURIComponent(pathname)}`); } }); return () => { alive = false; }; }, [locale, pathname, router, t]);
  useEffect(() => {
    const invalidate = () => {
      setIdentity(null);
      router.replace(`/${locale}/login?next=${encodeURIComponent(pathname)}`);
    };
    window.addEventListener(BLANK_AUTH_INVALIDATED_EVENT, invalidate);
    return () => window.removeEventListener(BLANK_AUTH_INVALIDATED_EVENT, invalidate);
  }, [locale, pathname, router]);
  useEffect(() => { const inSettings = pathname.includes("/settings/"); if (inSettings && !wasInSettings.current) setPanel("settings"); if (!inSettings) setPanel(null); wasInSettings.current = inSettings; }, [pathname]);
  const canNotifications = Boolean(identity?.permissions.has("notification.center.view"));
  useEffect(() => { if (!canNotifications) return; const timer = window.setTimeout(() => void refreshNotifications(), 0); return () => window.clearTimeout(timer); }, [canNotifications, refreshNotifications]);
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
    return { groups: [{ key: "main", nodes: [{ kind: "link", link: { key: "dashboard", label: t.navigation.dashboard, href: href("/app"), active: active("/app") } }] }, ...(settingsItems.length ? [{ key: "system", divider: true, nodes: [{ kind: "panel" as const, panel: { id: "settings", label: t.navigation.settings, active: pathname.includes("/settings/"), firstHref: settingsItems[0].href, items: settingsItems } }] }] : [])] };
  }, [active, activePrefix, canSecurity, href, identity, pathname, t]);
  const renderLink: RenderNavLink = ({ href: target, active: isActive, className, testId, onNavigate, children: label }) => <Link href={target} aria-current={isActive ? "page" : undefined} className={className} data-test-id={testId} onClick={onNavigate}>{label}</Link>;
  const openPanel = (next: NavPanel) => { setPanel(next.id); router.push(next.firstHref); };
  if (!identity) return <main className="mx-auto w-full max-w-6xl p-6" data-test-id="blank-auth-loading"><PageLoadingSkeleton/></main>;
  const topbar = <Topbar brand={
    <EnterpriseBrandSlot href={href("/app")} title={brand.title} subtitle={brand.subtitle} logoSrc={brand.logoSrc} testId="app-brand" renderLink={({ href: target, className, children: label, testId }) => <Link href={target} className={className} data-test-id={testId}>{label}</Link>}/>
  } actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={t.shell} notifications={canNotifications ? { items: notifications, loading: notificationsLoading, error: notificationsError, viewAllHref: href("/app/notifications"), onOpen: () => void refreshNotifications(), onDismiss: async (id) => { setNotifications((current) => current.filter((item) => item.id !== id)); await dismissNotification(id).catch(() => void refreshNotifications()); }, onDismissAll: async () => { setNotifications([]); await dismissAllNotifications().catch(() => void refreshNotifications()); } } : undefined} user={identity ? { name: identity.name, identity: identity.identity, avatarUrl: identity.avatarUrl, permissionSummary: t.common.permissionCount(identity.permissions.size) } : undefined} securityHref={canSecurity ? href("/app/settings/security") : undefined} renderLink={({ href: target, className, testId, role, children: label }) => <Link href={target} className={className} data-test-id={testId} role={role}>{label}</Link>} onLogout={() => performEnterpriseLogout(enterpriseLogoutAdapter, () => router.replace(`/${locale}/logged-out`))}/>} />;
  const forcedTarget = `/${locale}/app/settings/security/password`;
  if (identity.mustChangePassword && pathname === forcedTarget) return <BlankShellIdentityContext.Provider value={identity}><EnterprisePublicShell topbar={topbar} footer={footer}>{children}</EnterprisePublicShell></BlankShellIdentityContext.Provider>;
  const openPanelId = pathname.includes("/settings/") ? panel : null;
  return <BlankShellIdentityContext.Provider value={identity}><MotionConfig reducedMotion="user"><EnterpriseAppFrame topbar={topbar} sidebar={<Sidebar model={model} openPanelId={openPanelId} onOpenPanel={openPanel} onBack={() => setPanel(null)} renderLink={renderLink} backLabel={t.navigation.backToMain} navLabel={t.navigation.menu}/>} mobileNav={<MobileNav model={model} renderLink={renderLink} backLabel={t.navigation.backToMain} menuLabel={t.navigation.menu} closeLabel={t.navigation.close} navLabel={t.navigation.menu} pathKey={pathname}/>} footer={footer} mainClassName="pb-12">{children}</EnterpriseAppFrame></MotionConfig></BlankShellIdentityContext.Provider>;
}

function localizedLocation(pathname: string, locale: string, nextLocale: string) {
  const query = typeof window === "undefined" ? "" : window.location.search + window.location.hash;
  return `${pathname.replace(`/${locale}`, `/${nextLocale}`)}${query}`;
}
