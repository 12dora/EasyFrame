import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { primeEnterpriseGeneralSettings, resetEnterpriseGeneralSettings } from "@easy-enterprise/ui/enterprise";
import type { ShellIdentity } from "../lib/shell-adapter";

/**
 * 外壳导航速度的三条约束:侧栏链接不随进入视口预取、悬停 / 聚焦时才预取;
 * 点击的那一项在路由提交之前就画成选中态(导航意图);通知数据只重画顶栏,
 * 不牵动外壳里的页面。
 */

// `route.pathname` 就是被 mock 的 `usePathname()`:用例先点击、再把它改成目标路径重渲染,
// 模拟 App Router 「点击 → RSC 落地」之间的那段空窗。
const { useShellIdentity, prefetch, loadNotifications, sidebarRenders, route } = vi.hoisted(() => ({ useShellIdentity: vi.fn(), prefetch: vi.fn(), loadNotifications: vi.fn(), sidebarRenders: { count: 0 }, route: { pathname: "/zh-CN/app" } }));

vi.mock("./use-shell-identity", async (importOriginal) => ({ ...(await importOriginal<typeof import("./use-shell-identity")>()), useShellIdentity }));
vi.mock("../lib/shell-adapter", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/shell-adapter")>()), loadNotifications }));
// 空闲调度立即执行,用例不必等 requestIdleCallback。
vi.mock("../lib/identity-cache", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/identity-cache")>()), scheduleWhenIdle: (run: () => void) => { run(); return () => undefined; } }));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname, useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch }) }));
// `prefetch` 落成 data 属性,用例据此确认侧栏链接不随进入视口预取。
vi.mock("next/link", () => ({
  default: ({ href, children, prefetch: mode, ...rest }: { href: string; children: unknown; prefetch?: boolean | null } & Record<string, unknown>) => (
    <a href={href} data-prefetch={String(mode)} {...rest}>{children as never}</a>
  ),
}));
vi.mock("../assets/brand/jiefa_logo.webp", () => ({ default: { src: "/logo.webp" } }));
vi.mock("./antd-provider", () => ({ BlankAntdProvider: ({ children }: { children: unknown }) => children }));
// EasyUI 的动效组件在这条 vitest 车道上会解析到第二份 react:侧栏 / 顶栏 / 框架换成只保留
// 宿主接线点(renderLink、面板入口的 testId、通知属性)的替身,验的是本宿主的接法。
vi.mock("@easy-enterprise/ui/shell", async () => {
  const { createElement: h, Fragment } = await import("react");
  // `useNavIntent` 是纯逻辑(EasyUI 不引 next/*),用真的那份——用例要钉的正是宿主的接法。
  const { useNavIntent } = await vi.importActual<typeof import("@easy-enterprise/ui/shell")>("@easy-enterprise/ui/shell");
  type Node = { kind: "link"; link: { key: string; href: string; active: boolean; testId?: string; label: string } } | { kind: "panel"; panel: { id: string; testId?: string; label: string; firstHref: string } };
  const Nav = ({ model, renderLink, onOpenPanel }: { model: { groups: Array<{ key: string; nodes: Node[] }> }; renderLink: (props: Record<string, unknown>) => unknown; onOpenPanel?: (panel: unknown) => void }) => (sidebarRenders.count += 1) && h("nav", null, model.groups.flatMap((group) => group.nodes.map((node) => node.kind === "link"
    ? h(Fragment, { key: node.link.key }, renderLink({ href: node.link.href, active: node.link.active, className: "", testId: node.link.testId, onNavigate: () => undefined, children: node.link.label }) as never)
    : h("button", { key: node.panel.id, type: "button", "data-test-id": node.panel.testId, onClick: () => onOpenPanel?.(node.panel) }, node.panel.label))));
  return { Sidebar: Nav, MobileNav: () => null, Topbar: ({ brand, actions }: { brand: unknown; actions: unknown }) => h("header", null, brand as never, actions as never), useNavIntent };
});
vi.mock("@easy-enterprise/ui/enterprise", async (importOriginal) => {
  const { createElement: h } = await import("react");
  return {
    ...(await importOriginal<typeof import("@easy-enterprise/ui/enterprise")>()),
    // `aria-busy` / `data-pending-label` 照 EasyUI `AppShell` 的接法转发,用例据此确认宿主喂了 pending。
    EnterpriseAppFrame: ({ topbar, sidebar, children, pending, pendingLabel }: { topbar: unknown; sidebar: unknown; children: unknown; pending?: boolean; pendingLabel?: string }) => h("div", null, topbar as never, h("aside", null, sidebar as never), h("main", { "aria-busy": pending || undefined, "data-pending-label": pendingLabel }, children as never)),
    EnterpriseTopbarActions: ({ notifications }: { notifications?: { items: unknown[]; loading: boolean } }) => h("div", { "data-test-id": "topbar-notifications", "data-count": notifications ? notifications.items.length : -1, "data-loading": String(notifications?.loading ?? false) }),
  };
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
    permissions: new Set(["settings.app_setting.update", "notification.center.view"]),
    securityCapabilities: CAPABILITIES_OFF,
    accountId: "u1",
    isLocalSuperadmin: false,
    permissionRequestUrl: null,
    ...overrides,
  };
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  sidebarRenders.count = 0;
  route.pathname = "/zh-CN/app";
  // 默认不落地,免得通知回包在用例结束后才更新顶栏。
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

function render(value: ShellIdentity) {
  useShellIdentity.mockReturnValue({ identity: value, permissionUrlPending: false, refreshIdentity: vi.fn() });
  act(() => root.render(<BlankShell locale="zh-CN"><div data-test-id="shell-child">child</div></BlankShell>));
}

/** 路由真正落地:改 `route.pathname` 再重渲染,等同 App Router 提交新 RSC 后 `usePathname()` 翻页。 */
function commitRoute(pathname: string) {
  route.pathname = pathname;
  act(() => root.render(<BlankShell locale="zh-CN"><div data-test-id="shell-child">child</div></BlankShell>));
}

function click(element: HTMLElement | null) {
  // 不用 `element.click()`:mock 的 `next/link` 落成真 `<a href>`,happy-dom 会当成整页跳转。
  act(() => { element?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); });
}

function byTestId(testId: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-test-id="${testId}"]`);
}

describe("BlankShell navigation prefetch", () => {
  it("turns viewport prefetch off and prefetches a nav link on hover and focus", () => {
    render(identity());
    const link = byTestId("blank-nav-examples-table");
    expect(link?.dataset.prefetch).toBe("false");
    act(() => { link?.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: document.body })); });
    expect(prefetch).toHaveBeenCalledWith("/zh-CN/app/examples/table");
    prefetch.mockClear();
    act(() => { link?.focus(); });
    expect(prefetch).toHaveBeenCalledWith("/zh-CN/app/examples/table");
  });

  it("prefetches the settings landing page when the drill-down entry is hovered", () => {
    render(identity());
    const entry = byTestId("blank-nav-settings");
    expect(entry).not.toBeNull();
    act(() => { entry?.dispatchEvent(new PointerEvent("pointerover", { bubbles: true })); });
    expect(prefetch).toHaveBeenCalledWith("/zh-CN/app/settings/general");
  });
});

describe("BlankShell navigation intent", () => {
  it("marks the clicked entry active before the pathname commits", () => {
    render(identity());
    expect(byTestId("blank-nav-examples-table")?.getAttribute("aria-current")).toBeNull();
    expect(byTestId("blank-nav-dashboard")?.getAttribute("aria-current")).toBe("page");
    click(byTestId("blank-nav-examples-table"));
    // `usePathname()` 还停在旧路由,标记已经挪过去了。
    expect(route.pathname).toBe("/zh-CN/app");
    expect(byTestId("blank-nav-examples-table")?.getAttribute("aria-current")).toBe("page");
    expect(byTestId("blank-nav-dashboard")?.getAttribute("aria-current")).toBeNull();
  });

  it("marks <main> busy while the navigation is in flight and clears it on commit", () => {
    render(identity());
    const main = () => container.querySelector("main");
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    click(byTestId("blank-nav-examples-table"));
    expect(main()?.getAttribute("aria-busy")).toBe("true");
    expect(main()?.getAttribute("data-pending-label")).toBe("正在加载");
    commitRoute("/zh-CN/app/examples/table");
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(byTestId("blank-nav-examples-table")?.getAttribute("aria-current")).toBe("page");
  });

  it("stays idle when the current page is clicked again", () => {
    render(identity());
    click(byTestId("blank-nav-dashboard"));
    expect(container.querySelector("main")?.getAttribute("aria-busy")).toBeNull();
  });
});

describe("BlankShell notifications isolation", () => {
  // 外壳(侧栏与页面所在的那一层)不因通知加载态翻转而重画。
  it("loads notifications without re-rendering the shell", async () => {
    let settle: (items: unknown[]) => void = () => undefined;
    loadNotifications.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    render(identity());
    expect(byTestId("shell-child")).not.toBeNull();
    expect(loadNotifications).toHaveBeenCalledTimes(1);
    const rendersBefore = sidebarRenders.count;
    await act(async () => { settle([{ id: "n1", title: "t", detail: "d", urgent: false }]); await Promise.resolve(); });
    expect(byTestId("topbar-notifications")?.dataset.count).toBe("1");
    expect(sidebarRenders.count).toBe(rendersBefore);
  });

  it("does not fetch notifications without the permission", () => {
    render(identity({ permissions: new Set(["settings.app_setting.update"]) }));
    expect(loadNotifications).not.toHaveBeenCalled();
    expect(byTestId("topbar-notifications")?.dataset.count).toBe("-1");
  });
});
