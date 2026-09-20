import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellIdentity, ShellIdentityLoad, ShellSession } from "../lib/shell-adapter";

/**
 * The shell identity (perceived loading).
 *
 * Three things: `/auth/me` alone releases the identity (a slow `/auth/session` no longer pins the
 * page), this tab's snapshot gives the second visit a shell straight away, and the live identity
 * reconciles by `accountId`. The forced-password-change gate and the eject-to-login fallback are
 * unchanged.
 */

const { startShellIdentityLoad } = vi.hoisted(() => ({ startShellIdentityLoad: vi.fn() }));
const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("../lib/shell-adapter", () => ({ startShellIdentityLoad }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn() }) }));

const { useShellIdentity, onboardingReady } = await import("./use-shell-identity");
const { clearCachedIdentity, writeCachedIdentity } = await import("../lib/identity-cache");
const { AUTH_TOKEN_STORAGE_KEY } = await import("../lib/auth-adapter");
const { BLANK_AUTH_INVALIDATED_EVENT } = await import("../lib/platform-api");
type Locale = "zh-CN" | "en";

const CACHE_KEY = "blank.shell.identity";
const LABELS = { admin: "管理员", user: "用户", guest: "游客", separator: "、" };

const CAPABILITIES_OFF = {
  passwordChange: false,
  totpStatus: false,
  totpEnroll: false,
  totpDisable: false,
  passkeyList: false,
  passkeyRegister: false,
  passkeyDelete: false,
};

function identity(overrides: Partial<ShellIdentity> = {}): ShellIdentity {
  return {
    name: "张三",
    identity: "用户",
    identityKind: "user",
    email: "zhangsan@example.com",
    avatarUrl: null,
    hasLocalPassword: true,
    mustChangePassword: false,
    permissions: new Set(["ops.upstream_health.view"]),
    securityCapabilities: CAPABILITIES_OFF,
    accountId: "u1",
    isLocalSuperadmin: false,
    permissionRequestUrl: null,
    tableDensity: "compact",
    rowSpacing: "compact",
    ...overrides,
  };
}

/** `/auth/me` and `/auth/session` controlled separately. */
function stubLoad(value: ShellIdentity): { settleSession: (url: string | null, tableDensity?: ShellSession["tableDensity"]) => void } {
  let settle: (session: ShellSession) => void = () => undefined;
  const session = new Promise<ShellSession>((resolve) => { settle = resolve; });
  startShellIdentityLoad.mockResolvedValue({ identity: value, session } satisfies ShellIdentityLoad);
  return { settleSession: (url, tableDensity = "compact") => settle({ permissionRequestUrl: url, tableDensity, rowSpacing: "compact" }) };
}

let refresh: () => Promise<void> = () => Promise.resolve();

function Probe({ locale = "zh-CN", pathname = "/zh-CN/app" }: { locale?: Locale; pathname?: string }) {
  const { identity: value, permissionUrlPending, refreshIdentity } = useShellIdentity({ locale, pathname, fallbackName: "Fallback", identityLabels: LABELS });
  refresh = refreshIdentity;
  return (
    <div
      data-test-id="probe"
      data-name={value?.name ?? ""}
      data-account={value?.accountId ?? ""}
      data-url={value?.permissionRequestUrl ?? ""}
      data-density={value?.tableDensity ?? ""}
      data-pending={String(permissionUrlPending)}
    />
  );
}

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  clearCachedIdentity();
  window.sessionStorage.clear();
  window.localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(pathname?: string) {
  act(() => root.render(<Probe pathname={pathname} />));
}

/** Re-render the same tree with a new path / locale — that is what a navigation looks like. */
function navigate(locale: Locale, pathname: string) {
  act(() => root.render(<Probe locale={locale} pathname={pathname} />));
}

function probe(): HTMLElement {
  const element = container.querySelector<HTMLElement>('[data-test-id="probe"]');
  if (!element) throw new Error("probe is not mounted");
  return element;
}

/** Let the pending promise callbacks (the `then` chain inside the effect) run. */
async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe("useShellIdentity", () => {
  it("hands over the identity before /auth/session settles", async () => {
    const { settleSession } = stubLoad(identity());
    render();
    await flush();
    expect(probe().dataset.name).toBe("张三");
    expect(probe().dataset.pending).toBe("true");
    expect(probe().dataset.url).toBe("");

    await act(async () => { settleSession("https://easyauth.test/request"); await Promise.resolve(); });
    expect(probe().dataset.url).toBe("https://easyauth.test/request");
    expect(probe().dataset.pending).toBe("false");
  });

  // The density is an account preference carried by `/auth/session`: the snapshot's step holds the
  // screen until the session lands, then the server's answer wins.
  it("takes the account density from the settled session", async () => {
    writeCachedIdentity("zh-CN", identity({ tableDensity: "comfortable" }));
    const { settleSession } = stubLoad(identity());
    render();
    await flush();
    expect(probe().dataset.density).toBe("comfortable");

    await act(async () => { settleSession(null, "compact"); await Promise.resolve(); });
    expect(probe().dataset.density).toBe("compact");
  });

  // Snapshot reuse: the second page opened in this tab no longer waits for `/auth/me`.
  it("renders this tab's snapshot before the live identity answers", async () => {
    writeCachedIdentity("zh-CN", identity({ name: "快照用户", permissionRequestUrl: "https://easyauth.test/request" }));
    startShellIdentityLoad.mockReturnValue(new Promise(() => undefined));
    render();
    await flush();
    expect(probe().dataset.name).toBe("快照用户");
    expect(probe().dataset.url).toBe("https://easyauth.test/request");
  });

  // The snapshot supplies the request url while `/auth/session` is in flight, but a settled null
  // clears it — otherwise an un-configured url would come back on every same-tab reload.
  it("clears a stale request url once the session answers with null", async () => {
    writeCachedIdentity("zh-CN", identity({ permissionRequestUrl: "https://easyauth.test/stale" }));
    const { settleSession } = stubLoad(identity());
    render();
    await flush();
    expect(probe().dataset.url).toBe("https://easyauth.test/stale");

    await act(async () => { settleSession(null); await Promise.resolve(); });
    expect(probe().dataset.url).toBe("");
    expect(probe().dataset.pending).toBe("false");
  });

  it("reconciles the snapshot with the live identity for the same account", async () => {
    writeCachedIdentity("zh-CN", identity({ name: "旧名字" }));
    stubLoad(identity({ name: "新名字" }));
    render();
    await flush();
    expect(probe().dataset.name).toBe("新名字");
    expect(probe().dataset.account).toBe("u1");
  });

  it("replaces the snapshot when upstream says it is another user", async () => {
    writeCachedIdentity("zh-CN", identity({ name: "张三", accountId: "u1" }));
    stubLoad(identity({ name: "李四", accountId: "u2" }));
    render();
    await flush();
    expect(probe().dataset.name).toBe("李四");
    expect(probe().dataset.account).toBe("u2");
  });

  it("keeps the forced password change gate without a fresh request", async () => {
    stubLoad(identity({ mustChangePassword: true }));
    render("/zh-CN/app");
    await flush();
    expect(probe().dataset.name).toBe("");
    expect(replace).toHaveBeenCalledWith("/zh-CN/app/settings/security/password");
    // A blocked account never reaches the snapshot.
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
  });

  it("gives the identity on the password page itself", async () => {
    stubLoad(identity({ mustChangePassword: true }));
    render("/zh-CN/app/settings/security/password");
    await flush();
    expect(probe().dataset.name).toBe("张三");
    expect(replace).not.toHaveBeenCalled();
  });

  it("ejects to the login page when /auth/me fails", async () => {
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, "token-a");
    startShellIdentityLoad.mockRejectedValue(new Error("boom"));
    render();
    await flush();
    expect(replace).toHaveBeenCalledWith(`/zh-CN/login?next=${encodeURIComponent("/zh-CN/app")}`);
    expect(window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)).toBeNull();
    // The snapshot goes with it: the next person must not see the previous one's shell.
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
    expect(probe().dataset.pending).toBe("false");
  });

  it("ejects on the platform's auth-invalidated event", async () => {
    stubLoad(identity());
    render();
    await flush();
    expect(window.sessionStorage.getItem(CACHE_KEY)).not.toBeNull();
    act(() => { window.dispatchEvent(new Event(BLANK_AUTH_INVALIDATED_EVENT)); });
    expect(replace).toHaveBeenCalledWith(`/zh-CN/login?next=${encodeURIComponent("/zh-CN/app")}`);
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
  });

  // Yielding only makes sense when something is on screen: after a locale switch nothing is
  // readable (snapshots are keyed by locale), so giving up another idle window would just keep the
  // skeleton up for 1.2s longer.
  it("reloads the identity immediately when no snapshot can carry the screen", async () => {
    stubLoad(identity());
    render("/zh-CN/app");
    await flush();
    expect(startShellIdentityLoad).toHaveBeenCalledTimes(1);

    // Same locale, another page: the snapshot is still painting, so the routine re-check is
    // deferred to the idle window and this frame issues no request.
    navigate("zh-CN", "/zh-CN/app/settings/security");
    expect(startShellIdentityLoad).toHaveBeenCalledTimes(1);

    // Locale switch: no snapshot to carry the screen, re-fetch at once.
    navigate("en", "/en/app/settings/security");
    expect(startShellIdentityLoad).toHaveBeenCalledTimes(2);
    // Let that re-fetch land so React does not complain about an update outside act().
    await flush();
  });

  // Another tab logged out (the token was cleared): this page still paints that person's shell
  // while the credential is gone.
  it("drops the snapshot and reloads when another tab clears the token", async () => {
    const reload = vi.fn();
    Object.defineProperty(window.location, "reload", { value: reload, configurable: true });
    stubLoad(identity());
    render();
    await flush();
    expect(window.sessionStorage.getItem(CACHE_KEY)).not.toBeNull();

    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: AUTH_TOKEN_STORAGE_KEY, oldValue: "token-a", newValue: null }));
    });
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  // `EnterprisePermissionOnboarding` awaits `onRecheck` to drive its busy state: an explicit
  // refresh must resolve only once the re-fetch has landed, and must re-fetch immediately.
  it("resolves the explicit refresh when the re-fetch lands", async () => {
    stubLoad(identity({ name: "旧名字" }));
    render();
    await flush();
    expect(startShellIdentityLoad).toHaveBeenCalledTimes(1);

    const settled = vi.fn();
    stubLoad(identity({ name: "新名字" }));
    await act(async () => { void refresh().then(settled); });
    expect(startShellIdentityLoad).toHaveBeenCalledTimes(2);
    await flush();
    expect(settled).toHaveBeenCalledTimes(1);
    expect(probe().dataset.name).toBe("新名字");
  });

  it("ignores a storage event for another key or an unchanged value", async () => {
    const reload = vi.fn();
    Object.defineProperty(window.location, "reload", { value: reload, configurable: true });
    stubLoad(identity());
    render();
    await flush();
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "other", oldValue: "a", newValue: null }));
      window.dispatchEvent(new StorageEvent("storage", { key: AUTH_TOKEN_STORAGE_KEY, oldValue: "token-a", newValue: "token-a" }));
    });
    expect(reload).not.toHaveBeenCalled();
  });
});

/** The only screen that waits for `/auth/session`: its whole content is the request URL. */
describe("onboardingReady", () => {
  it("waits while the session is pending and nothing is known", () => {
    expect(onboardingReady(identity(), true)).toBe(false);
  });

  it("paints as soon as the snapshot already carries a request url", () => {
    expect(onboardingReady(identity({ permissionRequestUrl: "https://easyauth.test/request" }), true)).toBe(true);
  });

  it("paints once the session settled, even without a url", () => {
    expect(onboardingReady(identity(), false)).toBe(true);
  });
});
