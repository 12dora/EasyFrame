import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellIdentity } from "../lib/shell-adapter";

/**
 * 用户风险:行距是**账号偏好**,不是浏览器里的一份本地设置。它必须
 * (a) 从身份里来(`/auth/session` 的 `preferences`),(b) 点一下当场生效(等一趟往返再变
 * 会让人以为没点上),(c) 写回失败时退回原样并说一句话 —— 悄悄留在新档位等于骗人,
 * (d) 成功后写回身份快照,否则下一次身份复查会把刚改的档位顶回去,
 * (e) 只送 `rowSpacing` 这一个键:另一份偏好(表格行高)不许被这一次写回带偏。
 */

const api = vi.hoisted(() => ({ saveRowSpacing: vi.fn() }));
vi.mock("../lib/shell-adapter", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/shell-adapter")>()),
  saveRowSpacing: api.saveRowSpacing,
}));

const toasts = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock("@easy-enterprise/ui/toast", () => ({ toast: toasts }));
vi.mock("next/navigation", () => ({ usePathname: () => "/zh-CN/app/examples/table" }));

beforeAll(async () => { await import("./row-spacing"); }, 60_000);

const { BlankRowSpacingProvider } = await import("./row-spacing");
const { useRowSpacing } = await import("@easy-enterprise/ui");
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

/** 一个最小的消费者:把当前档位与写回口暴露成 DOM,全站表单读的就是这一份上下文。 */
function Probe() {
  const { rowSpacing, setRowSpacing, saving } = useRowSpacing();
  return (
    <button type="button" data-test-id="probe" data-spacing={rowSpacing} data-saving={String(saving)} onClick={() => void setRowSpacing("comfortable")}>
      {rowSpacing}
    </button>
  );
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  api.saveRowSpacing.mockReset();
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
  act(() => { root.render(<BlankRowSpacingProvider identity={value}>{<Probe />}</BlankRowSpacingProvider>); });
}

function probe(): HTMLElement {
  const element = container.querySelector<HTMLElement>('[data-test-id="probe"]');
  if (!element) throw new Error("probe is not mounted");
  return element;
}

describe("BlankRowSpacingProvider", () => {
  it("serves the spacing stored on the account, defaulting to compact", () => {
    render(identity());
    expect(probe().dataset.spacing).toBe("compact");
    render(identity({ rowSpacing: "comfortable" }));
    expect(probe().dataset.spacing).toBe("comfortable");
  });

  it("switches optimistically and writes the preference to the account", async () => {
    let settle: (value: unknown) => void = () => undefined;
    api.saveRowSpacing.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    render(identity());

    await act(async () => { probe().click(); });
    // 请求还在路上,界面已经换档了。
    expect(probe().dataset.spacing).toBe("comfortable");
    expect(probe().dataset.saving).toBe("true");
    expect(api.saveRowSpacing).toHaveBeenCalledWith("comfortable");

    await act(async () => { settle({ permissionRequestUrl: null, tableDensity: "compact", rowSpacing: "comfortable" }); });
    expect(probe().dataset.saving).toBe("false");
    expect(probe().dataset.spacing).toBe("comfortable");
    expect(toasts.error).not.toHaveBeenCalled();
  });

  // 两份偏好互不相干:改行距不许把身份快照里的行高带走。
  it("re-syncs the identity snapshot without disturbing the table density", async () => {
    writeCachedIdentity("zh-CN", identity({ tableDensity: "comfortable" }));
    api.saveRowSpacing.mockResolvedValue({ permissionRequestUrl: null, tableDensity: "comfortable", rowSpacing: "comfortable" });
    render(identity({ tableDensity: "comfortable" }));
    await act(async () => { probe().click(); });
    expect(readCachedIdentity("zh-CN")?.rowSpacing).toBe("comfortable");
    expect(readCachedIdentity("zh-CN")?.tableDensity).toBe("comfortable");
  });

  it("reverts and says so when the account write fails", async () => {
    api.saveRowSpacing.mockRejectedValue(new Error("offline"));
    render(identity());
    await act(async () => { probe().click(); });
    expect(probe().dataset.spacing).toBe("compact");
    expect(probe().dataset.saving).toBe("false");
    expect(toasts.error).toHaveBeenCalledWith(t.appearanceSettings.saveFailed, expect.anything());
  });

  it("never touches browser storage for the preference", async () => {
    api.saveRowSpacing.mockResolvedValue({ permissionRequestUrl: null, tableDensity: "compact", rowSpacing: "comfortable" });
    render(identity());
    await act(async () => { probe().click(); });
    const keys = [...Array(window.localStorage.length).keys()].map((index) => window.localStorage.key(index));
    expect(keys.filter((key) => key?.includes("spacing"))).toEqual([]);
  });
});
