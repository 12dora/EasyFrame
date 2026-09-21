import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { asyncDataGeneration, hasAsyncData, subscribeAsyncData, writeAsyncData, type AsyncDataEvent } from "./async-data-cache";
import { AUTH_TOKEN_STORAGE_KEY, endLocalSession, enterpriseLogoutAdapter, logout } from "./auth-adapter";
import {
  IDLE_TIMEOUT_MS,
  identitySessionGeneration,
  patchCachedPreference,
  clearCachedIdentity,
  readCachedIdentity,
  reconcileIdentity,
  sameIdentity,
  scheduleWhenIdle,
  subscribeCachedIdentity,
  writeCachedIdentity,
} from "./identity-cache";
import type { ShellIdentity } from "./shell-adapter";

/**
 * The per-tab identity snapshot: serialization round-trip, whole-snapshot rejection of bad data,
 * the reconcile rule, and the first-paint yield scheduler.
 */

const CACHE_KEY = "blank.shell.identity";

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
    securityCapabilities: { ...CAPABILITIES_OFF, passwordChange: true },
    accountId: "u1",
    isLocalSuperadmin: false,
    permissionRequestUrl: "https://easyauth.test/request",
    tableDensity: "compact",
    rowSpacing: "compact",
    ...overrides,
  };
}

beforeEach(() => {
  clearCachedIdentity();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("preference cache patches", () => {
  it("keeps the latest unrelated fields and the other preference", () => {
    writeCachedIdentity("zh-CN", identity());
    const generation = identitySessionGeneration();
    writeCachedIdentity("zh-CN", identity({ name: "新姓名", tableDensity: "comfortable" }));
    patchCachedPreference("zh-CN", "u1", generation, "rowSpacing", "comfortable");
    expect(readCachedIdentity("zh-CN")).toMatchObject({ name: "新姓名", tableDensity: "comfortable", rowSpacing: "comfortable" });
  });

  it("does not create a missing identity or patch another account or locale", () => {
    patchCachedPreference("zh-CN", "u1", identitySessionGeneration(), "rowSpacing", "comfortable");
    expect(readCachedIdentity("zh-CN")).toBeNull();
    writeCachedIdentity("zh-CN", identity({ accountId: "u2" }));
    patchCachedPreference("zh-CN", "u1", identitySessionGeneration(), "rowSpacing", "comfortable");
    patchCachedPreference("en", "u2", identitySessionGeneration(), "rowSpacing", "comfortable");
    expect(readCachedIdentity("zh-CN")?.rowSpacing).toBe("compact");
  });

  it("invalidates a request when the same account logs in again", () => {
    writeCachedIdentity("zh-CN", identity());
    const generation = identitySessionGeneration();
    clearCachedIdentity();
    writeCachedIdentity("zh-CN", identity());
    patchCachedPreference("zh-CN", "u1", generation, "rowSpacing", "comfortable");
    expect(readCachedIdentity("zh-CN")?.rowSpacing).toBe("compact");
  });
});

describe("identity snapshot", () => {
  it("round-trips an identity for the same locale", () => {
    writeCachedIdentity("zh-CN", identity());
    const cached = readCachedIdentity("zh-CN");
    expect(cached?.name).toBe("张三");
    expect(cached?.accountId).toBe("u1");
    expect([...(cached?.permissions ?? [])]).toEqual(["ops.upstream_health.view"]);
    expect(cached?.securityCapabilities.passwordChange).toBe(true);
    expect(cached?.securityCapabilities.totpEnroll).toBe(false);
    expect(cached?.permissionRequestUrl).toBe("https://easyauth.test/request");
    expect(cached?.tableDensity).toBe("compact");
  });

  // 行高是账号偏好,快照里也带着它:同一个标签页翻下一页时不该先闪一帧紧凑的表格。
  it("round-trips the account table density", () => {
    writeCachedIdentity("zh-CN", identity({ tableDensity: "comfortable" }));
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")?.tableDensity).toBe("comfortable");
  });

  // 行距是另一份独立的账号偏好,快照里同样带着它。
  it("round-trips the account row spacing", () => {
    writeCachedIdentity("zh-CN", identity({ rowSpacing: "comfortable" }));
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")?.rowSpacing).toBe("comfortable");
    // 两份偏好互不相干:只改行距,行高不动。
    expect(readCachedIdentity("zh-CN")?.tableDensity).toBe("compact");
  });

  // 加上行距之前写下的快照没有这个字段:整份仍可读,行距回落到全局默认。
  it("hydrates a snapshot without a row spacing to the default", () => {
    writeCachedIdentity("zh-CN", identity({ rowSpacing: "comfortable" }));
    const raw = JSON.parse(window.sessionStorage.getItem(CACHE_KEY)!) as { identity: Record<string, unknown> };
    delete raw.identity.rowSpacing;
    window.sessionStorage.setItem(CACHE_KEY, JSON.stringify(raw));
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")?.rowSpacing).toBe("compact");
  });

  // 结构版本(v2)之前写下的快照没有这个字段:整份仍可读,档位回落到全局默认。
  it("hydrates a snapshot without a density to the default", () => {
    writeCachedIdentity("zh-CN", identity());
    const raw = JSON.parse(window.sessionStorage.getItem(CACHE_KEY)!) as { identity: Record<string, unknown> };
    delete raw.identity.tableDensity;
    window.sessionStorage.setItem(CACHE_KEY, JSON.stringify(raw));
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")?.tableDensity).toBe("compact");
  });

  // The identity line and the fallback name are localized copy: the other language's copy is wrong.
  it("ignores a snapshot written under another locale", () => {
    writeCachedIdentity("zh-CN", identity());
    clearMemoryOnly();
    expect(readCachedIdentity("en")).toBeNull();
  });

  it("drops a corrupted snapshot instead of rendering half an identity", () => {
    window.sessionStorage.setItem(CACHE_KEY, "{not json");
    expect(readCachedIdentity("zh-CN")).toBeNull();
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
  });

  // 结构一变就整份作废,而不是读出半份身份:旧版本号的快照直接当没有。
  it("ignores a snapshot written by an older structure version", () => {
    writeCachedIdentity("zh-CN", identity());
    const raw = JSON.parse(window.sessionStorage.getItem(CACHE_KEY)!) as Record<string, unknown>;
    window.sessionStorage.setItem(CACHE_KEY, JSON.stringify({ ...raw, v: 1 }));
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")).toBeNull();
  });

  it("drops a snapshot whose contract fields are wrong", () => {
    window.sessionStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ v: 2, locale: "zh-CN", identity: { name: "张三", accountId: "u1", identityKind: "root", permissions: [] } }),
    );
    expect(readCachedIdentity("zh-CN")).toBeNull();
  });

  // A forced password change is a gate: current within this page load, never left for the next one.
  it("never persists a forced-password-change identity", () => {
    writeCachedIdentity("zh-CN", identity());
    writeCachedIdentity("zh-CN", identity({ mustChangePassword: true }));
    expect(readCachedIdentity("zh-CN")?.mustChangePassword).toBe(true);
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
    clearMemoryOnly();
    expect(readCachedIdentity("zh-CN")).toBeNull();
  });

  it("notifies subscribers on write and clear", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeCachedIdentity(listener);
    writeCachedIdentity("zh-CN", identity());
    clearCachedIdentity();
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    writeCachedIdentity("zh-CN", identity());
    expect(listener).toHaveBeenCalledTimes(2);
  });

  // Hard requirement of `useSyncExternalStore`: same content must return the same reference.
  it("returns a stable snapshot reference while nothing changed", () => {
    writeCachedIdentity("zh-CN", identity());
    expect(readCachedIdentity("zh-CN")).toBe(readCachedIdentity("zh-CN"));
    clearMemoryOnly();
    const parsed = readCachedIdentity("zh-CN");
    expect(readCachedIdentity("zh-CN")).toBe(parsed);
  });

  // No side effects inside `getSnapshot`: notifying mid-render makes `useSyncExternalStore` retry.
  it("drops a corrupted snapshot without notifying subscribers mid-render", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeCachedIdentity(listener);
    window.sessionStorage.setItem(CACHE_KEY, "{not json");
    expect(readCachedIdentity("zh-CN")).toBeNull();
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("swallows a sessionStorage that throws on get and set", () => {
    // Private mode / storage disabled: reads and writes both throw. Throwing here is a dead shell.
    const storage = window.sessionStorage;
    const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
    Object.defineProperty(window, "sessionStorage", { value: broken, configurable: true });
    try {
      expect(() => writeCachedIdentity("zh-CN", identity())).not.toThrow();
      // The memory copy still works: we only failed to leave a draft for the next load.
      expect(readCachedIdentity("zh-CN")?.name).toBe("张三");
      clearCachedIdentity();
      expect(() => readCachedIdentity("zh-CN")).not.toThrow();
      expect(readCachedIdentity("zh-CN")).toBeNull();
    } finally {
      Object.defineProperty(window, "sessionStorage", { value: storage, configurable: true });
    }
  });
});

/**
 * Explicit end of session: logout and the password-change success path must drop the snapshot too.
 *
 * The snapshot is sessionStorage + module memory and login is a same-tab `router.push`: without
 * this the next person sees the previous one's shell and sidebar, and the page's first batch of
 * requests is issued for the previous person.
 */
describe("endLocalSession", () => {
  it("drops the snapshot along with the credential", () => {
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, "token-a");
    writeCachedIdentity("zh-CN", identity());
    endLocalSession();
    expect(readCachedIdentity("zh-CN")).toBeNull();
    expect(window.sessionStorage.getItem(CACHE_KEY)).toBeNull();
    expect(window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("is what the logout adapter goes through", () => {
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, "token-a");
    writeCachedIdentity("zh-CN", identity());
    enterpriseLogoutAdapter.clearLocalSession();
    expect(readCachedIdentity("zh-CN")).toBeNull();
    expect(window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)).toBeNull();
  });

  // Both password-change surfaces end the session and send the user back to the login page.
  it.each([
    "app/[locale]/app/settings/security/password/page.tsx",
    "app/[locale]/app/settings/security/page.tsx",
  ])("is the port the password-change success path uses (%s)", (file) => {
    // These pages are one long JSX line; rendering them would pull in the whole enterprise kit.
    // What is pinned here is the port they wire. (The runner's cwd is the vitest binary's package,
    // so resolve against this file instead.)
    expect(readFileSync(join(import.meta.dirname, "..", file), "utf8")).toContain("clearLocalSession: endLocalSession");
  });

  it("leaves the snapshot alone on the 401 path's logout()", () => {
    // The platform layer calls `logout()` on a 401; the shell must stay on screen there.
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, "token-a");
    writeCachedIdentity("zh-CN", identity());
    logout();
    expect(readCachedIdentity("zh-CN")?.name).toBe("张三");
    expect(window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)).toBeNull();
  });
});

/** 读取缓存(`useAsyncData`)跟着身份走:登出、被踢、换人都整份作废。 */
describe("async data cache follows the identity", () => {
  function seed() {
    writeAsyncData("/notifications?limit=100", { items: [] }, asyncDataGeneration());
    expect(hasAsyncData("/notifications?limit=100")).toBe(true);
  }

  function recordEvents(): { events: AsyncDataEvent[]; stop: () => void } {
    const events: AsyncDataEvent[] = [];
    return { events, stop: subscribeAsyncData((event) => { events.push(event); }) };
  }

  it("is cleared without a refetch when the session ends (logout, eject, password change)", () => {
    writeCachedIdentity("zh-CN", identity());
    seed();
    const { events, stop } = recordEvents();
    endLocalSession();
    stop();
    expect(hasAsyncData("/notifications?limit=100")).toBe(false);
    expect(events).toEqual([{ kind: "clear", refetch: false }]);
  });

  it("is cleared and refetched when another account replaces the identity", () => {
    writeCachedIdentity("zh-CN", identity());
    seed();
    const { events, stop } = recordEvents();
    writeCachedIdentity("zh-CN", identity({ name: "张三(改名)" }));
    expect(hasAsyncData("/notifications?limit=100")).toBe(true);
    writeCachedIdentity("zh-CN", identity({ accountId: "u2", name: "李四" }));
    stop();
    expect(hasAsyncData("/notifications?limit=100")).toBe(false);
    expect(events).toEqual([{ kind: "clear", refetch: true }]);
  });
});

describe("reconcileIdentity", () => {
  it("takes the fresh identity when there is no snapshot", () => {
    const fresh = identity({ permissionRequestUrl: null });
    expect(reconcileIdentity(null, fresh)).toBe(fresh);
  });

  it("replaces the whole identity when the account changed", () => {
    const fresh = identity({ accountId: "u2", name: "李四", permissionRequestUrl: null });
    expect(reconcileIdentity(identity(), fresh)).toBe(fresh);
  });

  // Same person: keep the snapshot's URL until `/auth/session` answers, so the onboarding button
  // does not disappear and come back.
  it("keeps the snapshot request url until the session answers", () => {
    const merged = reconcileIdentity(identity(), identity({ name: "张三丰", permissionRequestUrl: null }));
    expect(merged.name).toBe("张三丰");
    expect(merged.permissionRequestUrl).toBe("https://easyauth.test/request");
  });

  it("lets a fresh request url win", () => {
    const merged = reconcileIdentity(identity(), identity({ permissionRequestUrl: "https://easyauth.test/new" }));
    expect(merged.permissionRequestUrl).toBe("https://easyauth.test/new");
  });

  // Once `/auth/session` has answered, its answer wins even when it is null — otherwise a URL that
  // has been un-configured would survive every same-tab reload via the snapshot.
  it("lets a settled session clear the snapshot request url", () => {
    const merged = reconcileIdentity(identity(), identity({ permissionRequestUrl: null }), true);
    expect(merged.permissionRequestUrl).toBeNull();
  });

  // 行高也来自 `/auth/session`:它落地之前沿用快照里的档位,否则选了「宽松」的人
  // 每次刷新都要先看一眼紧凑的表格再跳回去;落地之后以服务端的答案为准。
  it("keeps the snapshot density until the session answers", () => {
    const cached = identity({ tableDensity: "comfortable" });
    expect(reconcileIdentity(cached, identity({ tableDensity: "compact" })).tableDensity).toBe("comfortable");
    expect(reconcileIdentity(cached, identity({ permissionRequestUrl: null, tableDensity: "compact" })).tableDensity).toBe("comfortable");
    expect(reconcileIdentity(cached, identity({ tableDensity: "compact" }), true).tableDensity).toBe("compact");
  });

  // 行距走同一条规矩,而且与行高各算各的。
  it("keeps the snapshot row spacing until the session answers", () => {
    const cached = identity({ rowSpacing: "comfortable" });
    expect(reconcileIdentity(cached, identity({ rowSpacing: "compact" })).rowSpacing).toBe("comfortable");
    expect(reconcileIdentity(cached, identity({ permissionRequestUrl: null, rowSpacing: "compact" })).rowSpacing).toBe("comfortable");
    expect(reconcileIdentity(cached, identity({ rowSpacing: "compact" }), true).rowSpacing).toBe("compact");
  });
});

describe("sameIdentity", () => {
  it("accepts two equivalent identities", () => {
    expect(sameIdentity(identity(), identity())).toBe(true);
  });

  it("rejects a changed permission set", () => {
    expect(sameIdentity(identity(), identity({ permissions: new Set(["ops.upstream_health.view", "accounts.local.view"]) }))).toBe(false);
    expect(sameIdentity(identity(), identity({ permissions: new Set(["accounts.local.view"]) }))).toBe(false);
  });

  it("rejects a changed capability or label", () => {
    expect(sameIdentity(identity(), identity({ securityCapabilities: CAPABILITIES_OFF }))).toBe(false);
    expect(sameIdentity(identity(), identity({ identity: "管理员" }))).toBe(false);
  });

  // 换了档位就是屏幕上不一样:外壳必须重画,否则设置页改完列表还停在旧行高。
  it("rejects a changed table density", () => {
    expect(sameIdentity(identity(), identity({ tableDensity: "comfortable" }))).toBe(false);
  });

  // 行距同理:全站表单的松紧变了就是屏幕上不一样。
  it("rejects a changed row spacing", () => {
    expect(sameIdentity(identity(), identity({ rowSpacing: "comfortable" }))).toBe(false);
  });
});

describe("scheduleWhenIdle", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("runs the work after the idle budget and can be cancelled", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const cancel = scheduleWhenIdle(run);
    expect(run).not.toHaveBeenCalled();
    vi.advanceTimersByTime(IDLE_TIMEOUT_MS);
    expect(run).toHaveBeenCalledTimes(1);

    const second = vi.fn();
    scheduleWhenIdle(second)();
    vi.advanceTimersByTime(IDLE_TIMEOUT_MS);
    expect(second).not.toHaveBeenCalled();
    cancel();
  });

  // Real browsers take this branch (happy-dom has no requestIdleCallback): the deadline is
  // mandatory, otherwise a permanently busy page might never get to this work.
  it("uses requestIdleCallback with the budget as a deadline, and cancels through it", () => {
    const calls: { run: () => void; options: IdleRequestOptions | undefined }[] = [];
    const cancelled: number[] = [];
    vi.stubGlobal("requestIdleCallback", (run: () => void, options?: IdleRequestOptions) => { calls.push({ run, options }); return 7; });
    vi.stubGlobal("cancelIdleCallback", (handle: number) => { cancelled.push(handle); });
    try {
      const run = vi.fn();
      const cancel = scheduleWhenIdle(run);
      expect(calls).toHaveLength(1);
      expect(calls[0]?.options).toEqual({ timeout: IDLE_TIMEOUT_MS });
      expect(run).not.toHaveBeenCalled();
      calls[0]?.run();
      expect(run).toHaveBeenCalledTimes(1);
      cancel();
      expect(cancelled).toEqual([7]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

/** Clear only the memory copy, keeping sessionStorage — this is what "the next page load" reads. */
function clearMemoryOnly(): void {
  const raw = window.sessionStorage.getItem(CACHE_KEY);
  clearCachedIdentity();
  if (raw) window.sessionStorage.setItem(CACHE_KEY, raw);
}
