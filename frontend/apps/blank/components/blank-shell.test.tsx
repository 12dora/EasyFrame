import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { primeEnterpriseGeneralSettings, resetEnterpriseGeneralSettings } from "@easy-enterprise/ui/enterprise";
import { NAV_INTENT_TIMEOUT_MS, NAV_PROGRESS_DELAY_MS } from "@easy-enterprise/ui/shell";
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
// 侧栏 / 移动端导航 / 顶栏里的动效组件在这条 vitest 车道上会解析到第二份 react,换成替身;
// 替身保留宿主的全部接线点:renderLink(含 onNavigate)、面板入口 testId 与 `openPanelId`
// 驱动的子项、移动端抽屉的 `pathKey`。框架(EnterpriseAppFrame / AppShell / NavigationProgress)
// 不含动效依赖,**用真的**,aria-busy 与进度条因此验的是真实组件。
vi.mock("@easy-enterprise/ui/shell", async () => {
  const { createElement: h, Fragment } = await import("react");
  // `useNavIntent` 与两个时长常量是纯逻辑(EasyUI 不引 next/*),用真的那份——用例要钉的正是宿主的接法。
  const actual = await vi.importActual<typeof import("@easy-enterprise/ui/shell")>("@easy-enterprise/ui/shell");
  type Item = { key: string; href: string; active: boolean; testId?: string; label: string };
  type Node = { kind: "link"; link: Item } | { kind: "panel"; panel: { id: string; testId?: string; label: string; firstHref: string; items: Item[] } };
  type RenderLink = (props: Record<string, unknown>) => unknown;
  const leaf = (renderLink: RenderLink, item: Item) => h(Fragment, { key: item.key }, renderLink({ href: item.href, active: item.active, className: "", testId: item.testId, onNavigate: () => undefined, children: item.label }) as never);
  const Nav = ({ model, renderLink, onOpenPanel, openPanelId }: { model: { groups: Array<{ key: string; nodes: Node[] }> }; renderLink: RenderLink; onOpenPanel?: (panel: unknown) => void; openPanelId?: string | null }) => (sidebarRenders.count += 1) && h("nav", { "data-open-panel": openPanelId ?? undefined }, model.groups.flatMap((group) => group.nodes.map((node) => node.kind === "link"
    ? leaf(renderLink, node.link)
    : h("div", { key: node.panel.id },
      h("button", { type: "button", "data-test-id": node.panel.testId, onClick: () => onOpenPanel?.(node.panel) }, node.panel.label),
      openPanelId === node.panel.id ? node.panel.items.map((item) => leaf(renderLink, item)) : null,
    ))));
  const Mobile = ({ pathKey, variant }: { pathKey?: string; variant?: string }) => h("div", { "data-test-id": "mobile-nav", "data-path-key": pathKey, "data-variant": variant });
  // 顶栏替身要渲染 `leading`:手机汉堡就住在那个槽里(真实形态见 blank-shell.mobile.test.tsx)。
  return { ...actual, Sidebar: Nav, MobileNav: Mobile, Topbar: ({ brand, actions, leading }: { brand: unknown; actions: unknown; leading: unknown }) => h("header", null, leading as never, brand as never, actions as never) };
});
vi.mock("@easy-enterprise/ui/enterprise", async (importOriginal) => {
  const { createElement: h } = await import("react");
  return {
    ...(await importOriginal<typeof import("@easy-enterprise/ui/enterprise")>()),
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
    tableDensity: "compact",
    rowSpacing: "compact",
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

function click(element: HTMLElement | null, init: MouseEventInit = {}) {
  // 不用 `element.click()`:mock 的 `next/link` 落成真 `<a href>`,happy-dom 会当成整页跳转。
  act(() => { element?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init })); });
}

function byHref(href: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`a[href="${href}"]`);
}

const main = () => container.querySelector("main");
const current = (element: HTMLElement | null) => element?.getAttribute("aria-current") ?? null;
const DASHBOARD = "/zh-CN/app";
const EXAMPLES = "/zh-CN/app/examples/table";
const SETTINGS_GENERAL = "/zh-CN/app/settings/general";

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
  // 进度条与意图超时都靠定时器,这一组统一用假时钟。
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("marks the clicked entry active before the pathname commits", () => {
    render(identity());
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    click(byTestId("blank-nav-examples-table"));
    // `usePathname()` 还停在旧路由,标记已经挪过去了。
    expect(route.pathname).toBe(DASHBOARD);
    expect(current(byTestId("blank-nav-examples-table"))).toBe("page");
    expect(current(byTestId("blank-nav-dashboard"))).toBeNull();
  });

  // 带修饰键 / 中键的点击不会真导航(Next 的 Link 仍会调 onClick),记了意图就会让标记停在
  // 错误的条目上直到超时。
  it("ignores modified and non-primary clicks", () => {
    render(identity());
    click(byTestId("blank-nav-examples-table"), { ctrlKey: true });
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    click(byTestId("blank-nav-examples-table"), { button: 1 });
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    expect(main()?.getAttribute("aria-busy")).toBeNull();
  });

  it("marks <main> busy and shows the progress rail while the navigation is in flight", () => {
    render(identity());
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(byTestId("nav-progress")?.dataset.state).toBe("idle");
    click(byTestId("blank-nav-examples-table"));
    expect(main()?.getAttribute("aria-busy")).toBe("true");
    // 门槛之前一次都不闪。
    expect(byTestId("nav-progress")?.getAttribute("role")).toBeNull();
    act(() => { vi.advanceTimersByTime(NAV_PROGRESS_DELAY_MS); });
    expect(byTestId("nav-progress")?.getAttribute("role")).toBe("status");
    expect(byTestId("nav-progress")?.dataset.state).toBe("running");
    expect(byTestId("nav-progress")?.textContent).toBe("正在加载");
    commitRoute(EXAMPLES);
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(current(byTestId("blank-nav-examples-table"))).toBe("page");
    // 补满 + 淡出之后回 idle。
    expect(byTestId("nav-progress")?.dataset.state).toBe("done");
    act(() => { vi.advanceTimersByTime(200); });
    expect(byTestId("nav-progress")?.dataset.state).toBe("idle");
  });

  // 移动端抽屉靠 `pathKey` 关闭,必须等路由真的落地——换成意图路径会在点击瞬间关掉。
  it("keeps the mobile drawer key on the committed pathname", () => {
    render(identity());
    // 手机上只剩一条头部栏:导航是顶栏 leading 槽里的汉堡(trigger),不再是独立分区栏。
    expect(byTestId("mobile-nav")?.dataset.variant).toBe("trigger");
    expect(byTestId("mobile-nav")?.closest("header")).not.toBeNull();
    expect(byTestId("mobile-nav")?.dataset.pathKey).toBe(DASHBOARD);
    click(byTestId("blank-nav-examples-table"));
    expect(byTestId("mobile-nav")?.dataset.pathKey).toBe(DASHBOARD);
    commitRoute(EXAMPLES);
    expect(byTestId("mobile-nav")?.dataset.pathKey).toBe(EXAMPLES);
  });

  it("opens the settings panel and marks its first item before the route commits", () => {
    render(identity());
    click(byTestId("blank-nav-settings"));
    expect(container.querySelector("nav")?.dataset.openPanel).toBe("settings");
    expect(current(byHref(SETTINGS_GENERAL))).toBe("page");
    expect(route.pathname).toBe(DASHBOARD);
    expect(main()?.getAttribute("aria-busy")).toBe("true");
    commitRoute(SETTINGS_GENERAL);
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(container.querySelector("nav")?.dataset.openPanel).toBe("settings");
    // 点回主区:面板当帧收起,pending 要等新路由落地才清。
    click(byTestId("blank-nav-dashboard"));
    expect(container.querySelector("nav")?.dataset.openPanel).toBeUndefined();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    expect(main()?.getAttribute("aria-busy")).toBe("true");
    commitRoute(DASHBOARD);
    expect(main()?.getAttribute("aria-busy")).toBeNull();
  });

  it("lets the latest click win while a navigation is still pending", () => {
    render(identity());
    click(byTestId("blank-nav-settings"));
    expect(current(byHref(SETTINGS_GENERAL))).toBe("page");
    click(byTestId("blank-nav-examples-table"));
    expect(current(byTestId("blank-nav-examples-table"))).toBe("page");
    expect(byHref(SETTINGS_GENERAL)).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBeNull();
    expect(main()?.getAttribute("aria-busy")).toBe("true");
  });

  // 导航可能落在别处(重定向):意图让位给真实路由,而不是等超时。
  it("follows the committed path when it is not the clicked one", () => {
    render(identity());
    click(byTestId("blank-nav-examples-table"));
    commitRoute(SETTINGS_GENERAL);
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
    expect(container.querySelector("nav")?.dataset.openPanel).toBe("settings");
    expect(current(byHref(SETTINGS_GENERAL))).toBe("page");
  });

  // 导航被中止 / 出错时 `pathname` 永远不变,标记靠安全超时弹回真实路由。
  it("snaps the marker back to the real route after the intent times out", () => {
    render(identity());
    click(byTestId("blank-nav-examples-table"));
    expect(current(byTestId("blank-nav-examples-table"))).toBe("page");
    act(() => { vi.advanceTimersByTime(NAV_INTENT_TIMEOUT_MS); });
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    expect(main()?.getAttribute("aria-busy")).toBeNull();
  });

  it("stays idle when the current page is clicked again", () => {
    render(identity());
    click(byTestId("blank-nav-dashboard"));
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    // 点当前页还会撤掉没落地的旧意图(最后一次点击说了算)。
    click(byTestId("blank-nav-examples-table"));
    expect(main()?.getAttribute("aria-busy")).toBe("true");
    click(byTestId("blank-nav-dashboard"));
    expect(main()?.getAttribute("aria-busy")).toBeNull();
    expect(current(byTestId("blank-nav-dashboard"))).toBe("page");
    expect(current(byTestId("blank-nav-examples-table"))).toBeNull();
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
