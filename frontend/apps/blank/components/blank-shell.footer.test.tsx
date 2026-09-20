import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { primeEnterpriseGeneralSettings, resetEnterpriseGeneralSettings, type EnterpriseGeneralSettingsValue } from "@easy-enterprise/ui/enterprise";
import type { ShellIdentity } from "../lib/shell-adapter";

/**
 * 全局「显示页脚」(设置 → 外观)在外壳上的接线:
 * - 关掉时框底的页脚与手机抽屉里那份一起不画,`<main>` 仍是框里唯一的 `flex-1` 滚动区;
 * - 外壳段 `layout.tsx` 在 SSR 取到的通用设置首帧就生效(不会先画页脚再收起),
 *   但只作回退:客户端那次 GET 照发,落地后以它为准;
 * - 缓存里已有的值(登录页读过、设置页刚保存)比 SSR 交接优先;
 * - 外观页切换后(`primeEnterpriseGeneralSettings`)外壳当场收起 / 放出页脚;
 * - 强制改密壳(`EnterprisePublicShell`)跟同一个开关。
 */

const { useShellIdentity, loadGeneralSettings, loadNotifications, route } = vi.hoisted(() => ({ useShellIdentity: vi.fn(), loadGeneralSettings: vi.fn(), loadNotifications: vi.fn(), route: { pathname: "/zh-CN/app" } }));

vi.mock("./use-shell-identity", async (importOriginal) => ({ ...(await importOriginal<typeof import("./use-shell-identity")>()), useShellIdentity }));
vi.mock("../lib/shell-adapter", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/shell-adapter")>()), loadGeneralSettings, loadNotifications }));
vi.mock("../lib/identity-cache", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/identity-cache")>()), scheduleWhenIdle: (run: () => void) => { run(); return () => undefined; } }));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname, useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }) }));
vi.mock("next/link", () => ({
  // `prefetch` 不是 <a> 的属性,丢掉再透传。
  default: ({ href, children, ...rest }: { href: string; children: unknown } & Record<string, unknown>) => (
    <a href={href} {...Object.fromEntries(Object.entries(rest).filter(([key]) => key !== "prefetch"))}>{children as never}</a>
  ),
}));
vi.mock("../assets/brand/jiefa_logo.webp", () => ({ default: { src: "/logo.webp" } }));
vi.mock("./antd-provider", () => ({ BlankAntdProvider: ({ children }: { children: unknown }) => children }));
// 同 `blank-shell.mobile.test.tsx`:只换掉侧栏与顶栏动作区,框架、抽屉与页脚都用真的。
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
    rowSpacing: "compact",
    ...overrides,
  };
}

/** 旧后端:没有 `showFooter` 字段。 */
const LEGACY: EnterpriseGeneralSettingsValue = { titleZh: "", titleEn: "", subtitleZh: "", subtitleEn: "", footerHtmlZh: "", footerHtmlEn: "", logoDataUrl: null };
const HIDDEN = { ...LEGACY, showFooter: false };
const SHOWN = { ...LEGACY, showFooter: true };

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  resetEnterpriseGeneralSettings();
  route.pathname = "/zh-CN/app";
  loadNotifications.mockReturnValue(new Promise(() => undefined));
  // 默认让客户端读取一直挂着:用例里看到的值只能来自缓存或 SSR 交接。
  loadGeneralSettings.mockReturnValue(new Promise(() => undefined));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetEnterpriseGeneralSettings();
});

function render(initialGeneralSettings?: EnterpriseGeneralSettingsValue | null, overrides: Partial<ShellIdentity> = {}) {
  useShellIdentity.mockReturnValue({ identity: identity(overrides), permissionUrlPending: false, refreshIdentity: vi.fn() });
  act(() => root.render(
    <BlankShell locale="zh-CN" initialGeneralSettings={initialGeneralSettings}>
      <div data-test-id="shell-child">child</div>
    </BlankShell>,
  ));
}

function frameFooter(): Element | null {
  return container.querySelector('[data-test-id="app-footer-fallback"]');
}

/** 打开手机抽屉,返回抽屉底部的页脚区域(抽屉走 portal,挂在 `document.body` 上)。 */
function drawerFooterSlot(): Element | null {
  act(() => { document.querySelector<HTMLElement>('[data-test-id="admin-mobile-nav-trigger"]')?.click(); });
  return document.querySelector('[data-test-id="admin-mobile-nav-footer"]');
}

describe("BlankShell 显示页脚开关", () => {
  it("字段缺失(旧后端)时照常显示页脚", () => {
    primeEnterpriseGeneralSettings(LEGACY);
    render();
    expect(frameFooter()).not.toBeNull();
    expect(drawerFooterSlot()).not.toBeNull();
  });

  it("关掉时框底页脚与抽屉页脚一起不画,<main> 仍是 flex-1", () => {
    primeEnterpriseGeneralSettings(HIDDEN);
    render();
    expect(frameFooter()).toBeNull();
    expect(container.querySelector("footer")).toBeNull();
    // 抽屉拿不到 `footer` 就连那条 `border-t` 区域都不画。
    expect(drawerFooterSlot()).toBeNull();
    expect(container.querySelector("main")?.className.split(/\s+/)).toContain("flex-1");
  });

  it("外观页保存后不用刷新就跟着收起 / 放出", () => {
    primeEnterpriseGeneralSettings(SHOWN);
    render();
    expect(frameFooter()).not.toBeNull();
    act(() => primeEnterpriseGeneralSettings(HIDDEN));
    expect(frameFooter()).toBeNull();
    act(() => primeEnterpriseGeneralSettings(SHOWN));
    expect(frameFooter()).not.toBeNull();
  });

  it("SSR 交接首帧就生效,客户端 GET 落地后以它为准", async () => {
    let resolve: (value: EnterpriseGeneralSettingsValue) => void = () => undefined;
    loadGeneralSettings.mockReturnValue(new Promise((done) => { resolve = done; }));
    render(HIDDEN);
    expect(frameFooter()).toBeNull();
    expect(loadGeneralSettings).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(SHOWN); });
    expect(frameFooter()).not.toBeNull();
  });

  it("共享缓存里已有的值比 SSR 交接优先", () => {
    primeEnterpriseGeneralSettings(SHOWN);
    render(HIDDEN);
    expect(frameFooter()).not.toBeNull();
  });

  it("SSR 没取到时退回缺省:显示页脚,客户端自己读", () => {
    render(null);
    expect(frameFooter()).not.toBeNull();
    expect(loadGeneralSettings).toHaveBeenCalledTimes(1);
  });

  it("强制改密壳跟同一个开关", () => {
    route.pathname = "/zh-CN/app/settings/security/password";
    primeEnterpriseGeneralSettings(HIDDEN);
    render(undefined, { mustChangePassword: true });
    expect(container.querySelector('[data-test-id="shell-child"]')).not.toBeNull();
    expect(frameFooter()).toBeNull();
  });
});
