import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellIdentity } from "../lib/shell-adapter";

/**
 * 用户风险:行高是**账号偏好**,不是浏览器里的一份本地设置。它必须
 * (a) 从身份里来(`/auth/session` 的 `preferences`),(b) 点一下当场生效(等一趟往返再变
 * 会让人以为没点上),(c) 写回失败时退回原样并说一句话 —— 悄悄留在新档位等于骗人,
 * (d) 成功后写回身份快照,否则下一次身份复查会把刚改的档位顶回去。
 */

const api = vi.hoisted(() => ({ saveTableDensity: vi.fn() }));
vi.mock("../lib/shell-adapter", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/shell-adapter")>()),
  saveTableDensity: api.saveTableDensity,
}));

const toasts = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock("@easy-enterprise/ui/toast", () => ({ toast: toasts }));
vi.mock("next/navigation", () => ({ usePathname: () => "/zh-CN/app/examples/table" }));

beforeAll(async () => { await import("./table-density"); }, 60_000);

const { BlankTableDensityProvider } = await import("./table-density");
const { useTableDensity } = await import("@easy-enterprise/ui/table");
const { clearCachedIdentity, readCachedIdentity, writeCachedIdentity } = await import("../lib/identity-cache");
const { messages } = await import("../lib/messages");

const t = messages("zh-CN");

function identity(overrides: Partial<ShellIdentity> = {}): ShellIdentity {
  return {
    name: "张三",
    identity: "管理员",
    identityKind: "admin",
    email: null,
    avatarUrl: null,
    hasLocalPassword: true,
    mustChangePassword: false,
    permissions: new Set<string>(),
    securityCapabilities: {
      passwordChange: false, totpStatus: false, totpEnroll: false, totpDisable: false,
      passkeyList: false, passkeyRegister: false, passkeyDelete: false,
    },
    accountId: "u1",
    isLocalSuperadmin: false,
    permissionRequestUrl: null,
    tableDensity: "compact",
    rowSpacing: "compact",
    ...overrides,
  };
}

/** 一个最小的消费者:把当前档位与写回口暴露成 DOM,`DataTable` / `ClientTable` 读的就是这一份上下文。 */
function Probe() {
  const { density, setDensity, saving } = useTableDensity();
  return (
    <button type="button" data-test-id="probe" data-density={density} data-saving={String(saving)} onClick={() => void setDensity("comfortable")}>
      {density}
    </button>
  );
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  api.saveTableDensity.mockReset();
  for (const fn of Object.values(toasts)) fn.mockReset();
  clearCachedIdentity();
  window.sessionStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  clearCachedIdentity();
});

function render(value: ShellIdentity) {
  act(() => { root.render(<BlankTableDensityProvider identity={value}>{<Probe />}</BlankTableDensityProvider>); });
}

function probe(): HTMLElement {
  const element = container.querySelector<HTMLElement>('[data-test-id="probe"]');
  if (!element) throw new Error("probe is not mounted");
  return element;
}

describe("BlankTableDensityProvider", () => {
  it("serves the density stored on the account, defaulting to compact", () => {
    render(identity());
    expect(probe().dataset.density).toBe("compact");
    render(identity({ tableDensity: "comfortable" }));
    expect(probe().dataset.density).toBe("comfortable");
  });

  it("switches optimistically and writes the preference to the account", async () => {
    let settle: (value: unknown) => void = () => undefined;
    api.saveTableDensity.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    render(identity());

    await act(async () => { probe().click(); });
    // 请求还在路上,表格已经换档了。
    expect(probe().dataset.density).toBe("comfortable");
    expect(probe().dataset.saving).toBe("true");
    expect(api.saveTableDensity).toHaveBeenCalledWith("comfortable");

    await act(async () => { settle({ permissionRequestUrl: null, tableDensity: "comfortable" }); });
    expect(probe().dataset.saving).toBe("false");
    expect(probe().dataset.density).toBe("comfortable");
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it("re-syncs the identity snapshot so a later identity refresh cannot undo the change", async () => {
    writeCachedIdentity("zh-CN", identity());
    api.saveTableDensity.mockResolvedValue({ permissionRequestUrl: null, tableDensity: "comfortable" });
    render(identity());
    await act(async () => { probe().click(); });
    expect(readCachedIdentity("zh-CN")?.tableDensity).toBe("comfortable");
  });

  it("reverts and says so when the account write fails", async () => {
    api.saveTableDensity.mockRejectedValue(new Error("offline"));
    render(identity());
    await act(async () => { probe().click(); });
    expect(probe().dataset.density).toBe("compact");
    expect(probe().dataset.saving).toBe("false");
    expect(toasts.error).toHaveBeenCalledWith(t.appearanceSettings.saveFailed, expect.anything());
  });

  it("accepts later identity preferences after a successful save", async () => {
    api.saveTableDensity.mockResolvedValue({ tableDensity: "comfortable", rowSpacing: "comfortable" });
    render(identity());
    await act(async () => { probe().click(); });
    expect(probe().dataset.density).toBe("comfortable");
    render(identity({ tableDensity: "compact" }));
    expect(probe().dataset.density).toBe("compact");
  });

  it("allows only one write while this preference is saving", async () => {
    let settle!: (value: unknown) => void;
    api.saveTableDensity.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    render(identity());
    await act(async () => { probe().click(); });
    await act(async () => { probe().click(); });
    expect(api.saveTableDensity).toHaveBeenCalledTimes(1);
    await act(async () => { settle({ tableDensity: "comfortable", rowSpacing: "comfortable" }); });
    expect(probe().dataset.saving).toBe("false");
  });

  it.each(["clear", "switch", "same-account-login", "unmount"])("ignores a completion after %s", async (transition) => {
    let settle!: (value: unknown) => void;
    api.saveTableDensity.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    writeCachedIdentity("zh-CN", identity());
    render(identity());
    await act(async () => { probe().click(); });
    act(() => {
      if (transition === "unmount") root.render(null);
      else if (transition === "switch") {
        writeCachedIdentity("zh-CN", identity({ accountId: "u2" }));
        render(identity({ accountId: "u2" }));
      } else {
        clearCachedIdentity();
        if (transition === "same-account-login") writeCachedIdentity("zh-CN", identity());
      }
    });
    await act(async () => { settle({ tableDensity: "comfortable", rowSpacing: "comfortable" }); });
    const cached = readCachedIdentity("zh-CN");
    if (transition === "clear") expect(cached).toBeNull();
    else {
      expect(cached?.accountId).toBe(transition === "switch" ? "u2" : "u1");
      expect(cached?.tableDensity).toBe("compact");
    }
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it("drops old-account errors and immediately uses the new account preference", async () => {
    let reject!: (reason: Error) => void;
    api.saveTableDensity.mockReturnValue(new Promise((_resolve, fail) => { reject = fail; }));
    render(identity());
    await act(async () => { probe().click(); });
    render(identity({ accountId: "u2" }));
    expect(probe().dataset.density).toBe("compact");
    expect(probe().dataset.saving).toBe("false");
    await act(async () => { reject(new Error("old request failed")); });
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it("never touches browser storage for the preference", async () => {
    api.saveTableDensity.mockResolvedValue({ permissionRequestUrl: null, tableDensity: "comfortable" });
    render(identity());
    await act(async () => { probe().click(); });
    const keys = [...Array(window.localStorage.length).keys()].map((index) => window.localStorage.key(index));
    expect(keys.filter((key) => key?.includes("density"))).toEqual([]);
  });
});
