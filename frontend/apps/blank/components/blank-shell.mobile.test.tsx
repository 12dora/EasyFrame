import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { primeEnterpriseGeneralSettings, resetEnterpriseGeneralSettings } from "@easy-enterprise/ui/enterprise";
import { messages } from "../lib/messages";
import type { ShellIdentity } from "../lib/shell-adapter";

/**
 * 手机形态的外壳契约(`blank-shell.test.tsx` 把导航速度那一组钉在替身上,这一组反过来
 * 用**真的** `Topbar` / `MobileNav` / `EnterpriseAppFrame`,因为要验的正是三者的接法):
 *   - 手机上只剩一条头部栏 —— 汉堡在顶栏 `<header>` 里,旧的分区栏 `admin-mobile-nav` 不再渲染;
 *   - 抽屉打开后底部有页脚文案,而且那一份是 `bare` 的(抽屉是 role="dialog",
 *     再嵌一个 `<footer>` 会多出一个 contentinfo 地标);
 *   - 页面底部那一份页脚(地标)仍挂在框架上 —— 手机靠 CSS 隐藏,不是不传。
 */

const { useShellIdentity, loadNotifications, route } = vi.hoisted(() => ({ useShellIdentity: vi.fn(), loadNotifications: vi.fn(), route: { pathname: "/zh-CN/app" } }));

vi.mock("./use-shell-identity", async (importOriginal) => ({ ...(await importOriginal<typeof import("./use-shell-identity")>()), useShellIdentity }));
vi.mock("../lib/shell-adapter", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/shell-adapter")>()), loadNotifications }));
vi.mock("../lib/identity-cache", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/identity-cache")>()), scheduleWhenIdle: (run: () => void) => { run(); return () => undefined; } }));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname, useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }) }));
vi.mock("next/link", () => ({
  default: ({ href, children, prefetch: _mode, ...rest }: { href: string; children: unknown; prefetch?: boolean | null } & Record<string, unknown>) => (
    <a href={href} {...rest}>{children as never}</a>
  ),
}));
vi.mock("../assets/brand/jiefa_logo.webp", () => ({ default: { src: "/logo.webp" } }));
vi.mock("./antd-provider", () => ({ BlankAntdProvider: ({ children }: { children: unknown }) => children }));
// 只换掉侧栏(它的 AnimatePresence 在这条 vitest 车道上会解析到第二份 react);
// 顶栏、移动端导航与框架都用真的 —— 手机形态验的就是宿主怎么接它们。
vi.mock("@easy-enterprise/ui/shell", async (importOriginal) => {
  const { createElement: h } = await import("react");
  return { ...(await importOriginal<typeof import("@easy-enterprise/ui/shell")>()), Sidebar: () => h("nav", { "data-test-id": "desktop-sidebar" }) };
});
vi.mock("@easy-enterprise/ui/enterprise", async (importOriginal) => {
  const { createElement: h } = await import("react");
  return { ...(await importOriginal<typeof import("@easy-enterprise/ui/enterprise")>()), EnterpriseTopbarActions: () => h("div", { "data-test-id": "topbar-actions" }) };
});

const { BlankShell } = await import("./blank-shell");

const CAPABILITIES_OFF = { passwordChange: false, totpStatus: false, totpEnroll: false, totpDisable: false, passkeyList: false, passkeyRegister: false, passkeyDelete: false };

function identity(overrides: Partial<ShellIdentity> = {}): ShellIdentity {
  return {
    name: "Admin",
    identity: "管理员",
    identityKind: "admin",
    email: "admin@example.com",
    avatarUrl: null,
    hasLocalPassword: true,
    mustChangePassword: false,
    permissions: new Set(["settings.app_setting.update"]),
    securityCapabilities: CAPABILITIES_OFF,
    accountId: "u1",
    isLocalSuperadmin: false,
    permissionRequestUrl: null,
    tableDensity: "compact",
    ...overrides,
  };
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  route.pathname = "/zh-CN/app";
  loadNotifications.mockReturnValue(new Promise(() => undefined));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  primeEnterpriseGeneralSettings({ titleZh: "", titleEn: "", subtitleZh: "", subtitleEn: "", footerHtmlZh: "", footerHtmlEn: "", logoDataUrl: null });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetEnterpriseGeneralSettings();
});

function render(overrides: Partial<ShellIdentity> = {}) {
  useShellIdentity.mockReturnValue({ identity: identity(overrides), permissionUrlPending: false, refreshIdentity: vi.fn() });
  act(() => root.render(<BlankShell locale="zh-CN"><div data-test-id="shell-child">child</div></BlankShell>));
}

/** 抽屉走 portal 挂在 `document.body` 上,不在 `container` 里。 */
function inDocument(testId: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-test-id="${testId}"]`);
}

/**
 * 按 class **token 集合**断言,不用 `toContain`:`"pb-12"` 是 `"md:pb-12"` 的子串,
 * 子串断言分不出「手机上也留了 48px」和「只有桌面留 48px」。
 */
function classes(element: Element | null | undefined): Set<string> {
  return new Set((element?.getAttribute("class") ?? "").split(/\s+/).filter(Boolean));
}

const T = messages("zh-CN");

describe("BlankShell 手机头部栏", () => {
  it("汉堡按钮渲染在顶栏 <header> 里,旧的分区栏不再出现", () => {
    render();
    const trigger = inDocument("admin-mobile-nav-trigger");
    expect(trigger).not.toBeNull();
    expect(trigger?.closest("header")).not.toBeNull();
    // trigger 形态自带 md:hidden,桌面上不露出来。
    expect(classes(trigger).has("md:hidden")).toBe(true);
    expect(inDocument("admin-mobile-nav")).toBeNull();
    expect(inDocument("admin-mobile-nav-current")).toBeNull();
  });

  it("汉堡按钮带本地化的读屏名字与展开态", () => {
    render();
    const trigger = inDocument("admin-mobile-nav-trigger");
    expect(trigger?.getAttribute("aria-label")).toBe(T.navigation.menu);
    expect(trigger?.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger?.getAttribute("aria-expanded")).toBe("false");
    act(() => { trigger?.click(); });
    expect(inDocument("admin-mobile-nav-trigger")?.getAttribute("aria-expanded")).toBe("true");
  });

  it("正文底部留白手机 24px、桌面 48px", () => {
    const main = () => classes(container.querySelector("main"));
    render();
    expect(main().has("pb-6")).toBe(true);
    expect(main().has("md:pb-12")).toBe(true);
    // 关键的是「手机上不再是 48px」——光验 md:pb-12 的话,漏掉断点前缀也照样通过。
    expect(main().has("pb-12")).toBe(false);
  });
});

describe("BlankShell 抽屉页脚", () => {
  it("点汉堡打开抽屉,页脚文案在抽屉里且不带 <footer> 地标", () => {
    render();
    expect(inDocument("admin-mobile-nav-drawer")).toBeNull();
    act(() => { inDocument("admin-mobile-nav-trigger")?.click(); });
    const drawer = inDocument("admin-mobile-nav-drawer");
    expect(drawer).not.toBeNull();
    const inline = drawer?.querySelector<HTMLElement>('[data-test-id="app-footer-fallback-inline"]');
    expect(inline).not.toBeNull();
    expect(inline?.textContent).toContain("企业应用框架");
    // 抽屉本身已经是 role="dialog";再嵌一个 <footer> 就多出一个 contentinfo 地标。
    expect(drawer?.querySelector("footer")).toBeNull();
    expect(drawer?.querySelector('[data-test-id="app-footer-fallback"]')).toBeNull();
  });

  it("页面底部那一份页脚仍挂在框架上(手机靠 CSS 收起,不是不传)", () => {
    render();
    const landmark = container.querySelector<HTMLElement>('[data-test-id="app-footer-fallback"]');
    expect(landmark).not.toBeNull();
    const wrapper = classes(landmark?.closest("footer")?.parentElement);
    // 两个 token 都要:只有 `md:block` 而没有 `hidden`,手机上页脚照样在页面底部;
    // 只有 `hidden` 而没有 `md:block`,桌面上页脚就整条没了。
    expect(wrapper.has("hidden")).toBe(true);
    expect(wrapper.has("md:block")).toBe(true);
  });
});

describe("BlankShell 强制改密壳", () => {
  // 那一页只能改密码,没有导航模型可进:顶栏不带 `leading`,页脚还是页面级的那一份。
  it("不渲染汉堡与分区栏,页脚仍是带地标的那一份", () => {
    route.pathname = "/zh-CN/app/settings/security/password";
    render({ mustChangePassword: true });
    expect(inDocument("admin-mobile-nav-trigger")).toBeNull();
    expect(inDocument("admin-mobile-nav")).toBeNull();
    expect(inDocument("admin-mobile-nav-drawer")).toBeNull();
    const landmark = container.querySelector<HTMLElement>('[data-test-id="app-footer-fallback"]');
    expect(landmark).not.toBeNull();
    expect(landmark?.closest("footer")).not.toBeNull();
  });
});
