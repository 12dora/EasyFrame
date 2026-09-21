/**
 * Adapter-level pins for the shell identity line and the general-settings wire.
 * Focus: the identity label precedence (superadmin > role groups > any grant >
 * guest) and the `/app-settings/general` read/write shape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isNotificationManagedConflict } from "@easy-enterprise/ui/enterprise";

import { loadAuthSession, loadShellIdentity, loadGeneralSettings, notificationSettingsAdapter, saveGeneralSettings, saveRowSpacing, saveTableDensity, startShellIdentityLoad, type ShellGeneralSettings } from "./shell-adapter";

const labels = { admin: "管理员", user: "用户", guest: "游客", separator: "、" };

type FetchCall = [input: RequestInfo | URL, init?: RequestInit];

function respondWith(body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
}

beforeEach(() => {
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("labels a local superadmin as administrator even without role groups", async () => {
  vi.stubGlobal("fetch", respondWith({ id: "u1", name: "Admin", isLocalSuperadmin: true, permissions: ["settings.app_setting.update"] }));
  const identity = await loadShellIdentity("Fallback", labels);
  expect([identity.identity, identity.identityKind]).toEqual(["管理员", "admin"]);
});

it("joins role groups for a granted account", async () => {
  vi.stubGlobal("fetch", respondWith({ id: "u2", name: "User", roleGroups: ["运营", "财务"], permissions: ["ops.upstream_health.view"] }));
  const identity = await loadShellIdentity("Fallback", labels);
  expect([identity.identity, identity.identityKind]).toEqual(["运营、财务", "user"]);
});

it("falls back to the plain user label when only permissions are granted", async () => {
  vi.stubGlobal("fetch", respondWith({ id: "u3", name: "User", roleGroups: [], permissions: ["ops.upstream_health.view"] }));
  const identity = await loadShellIdentity("Fallback", labels);
  expect([identity.identity, identity.identityKind]).toEqual(["用户", "user"]);
});

it("labels an account with no grants as a guest", async () => {
  vi.stubGlobal("fetch", respondWith({ id: "u4", name: "", permissions: [] }));
  const identity = await loadShellIdentity("Fallback", labels);
  expect([identity.name, identity.identity, identity.identityKind, identity.email]).toEqual(["Fallback", "游客", "guest", null]);
});

it("reads the permission request URL from the login-gated session route", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/api/v1/auth/session")) {
      return new Response(JSON.stringify({ permissionRequestUrl: "  https://easyauth.example.test/request  " }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return new Response("{}", { status: 500 });
  });
  vi.stubGlobal("fetch", fetchMock);
  expect(await loadAuthSession()).toEqual({ permissionRequestUrl: "https://easyauth.example.test/request", tableDensity: "compact", rowSpacing: "compact" });
  expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/v1/auth/session");
});

it("treats a missing or failed session URL as null without logging out", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ permissionRequestUrl: "  " }), { status: 200, headers: { "content-type": "application/json" } })));
  expect(await loadAuthSession()).toEqual({ permissionRequestUrl: null, tableDensity: "compact", rowSpacing: "compact" });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
  expect(await loadAuthSession()).toEqual({ permissionRequestUrl: null, tableDensity: "compact", rowSpacing: "compact" });
});

/**
 * The account preference (table density): it rides in on `/auth/session` and is written back with
 * `PATCH /auth/preferences`. A backend that has not shipped the field yet still yields a definite
 * default — the contract says compact.
 */
it("hydrates the account table density, defaulting to compact", async () => {
  const session = (body: unknown) => vi.fn(async (input: RequestInfo | URL) => (String(input).includes("/api/v1/auth/session")
    ? new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
    : new Response("{}", { status: 500 })));

  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: { tableDensity: "comfortable" } }));
  expect((await loadAuthSession()).tableDensity).toBe("comfortable");

  // Unknown step, null and a missing `preferences` block all hydrate to the contract default.
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: { tableDensity: "roomy" } }));
  expect((await loadAuthSession()).tableDensity).toBe("compact");
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: null }));
  expect((await loadAuthSession()).tableDensity).toBe("compact");
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null }));
  expect((await loadAuthSession()).tableDensity).toBe("compact");
});

it("writes the density back with PATCH /auth/preferences and returns the updated session", async () => {
  const fetchMock = respondWith({ permissionRequestUrl: "https://easyauth.example.test/request", preferences: { tableDensity: "comfortable" } });
  vi.stubGlobal("fetch", fetchMock);

  await expect(saveTableDensity("comfortable")).resolves.toEqual({ permissionRequestUrl: "https://easyauth.example.test/request", tableDensity: "comfortable", rowSpacing: "compact" });

  const calls = fetchMock.mock.calls as unknown as FetchCall[];
  expect(String(calls[0][0])).toContain("/api/v1/auth/preferences");
  expect(calls[0][1]?.method).toBe("PATCH");
  // Only the changed key travels; the server owns everything else on the session.
  expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ tableDensity: "comfortable" });
});

/**
 * The second account preference (row spacing) rides the very same route and hydrates the same way.
 * It is independent of the density: the two must never read or write through each other.
 */
it("hydrates the account row spacing, defaulting to compact", async () => {
  const session = (body: unknown) => vi.fn(async (input: RequestInfo | URL) => (String(input).includes("/api/v1/auth/session")
    ? new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
    : new Response("{}", { status: 500 })));

  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: { rowSpacing: "comfortable" } }));
  expect((await loadAuthSession()).rowSpacing).toBe("comfortable");
  // The sibling preference is untouched by it.
  expect((await loadAuthSession()).tableDensity).toBe("compact");

  // Unknown step, null and a missing `preferences` block all hydrate to the contract default.
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: { rowSpacing: "roomy" } }));
  expect((await loadAuthSession()).rowSpacing).toBe("compact");
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null, preferences: null }));
  expect((await loadAuthSession()).rowSpacing).toBe("compact");
  vi.stubGlobal("fetch", session({ permissionRequestUrl: null }));
  expect((await loadAuthSession()).rowSpacing).toBe("compact");
});

it("writes the row spacing back with PATCH /auth/preferences and returns the updated session", async () => {
  const fetchMock = respondWith({ permissionRequestUrl: null, preferences: { tableDensity: "comfortable", rowSpacing: "comfortable" } });
  vi.stubGlobal("fetch", fetchMock);

  await expect(saveRowSpacing("comfortable")).resolves.toEqual({ permissionRequestUrl: null, tableDensity: "comfortable", rowSpacing: "comfortable" });

  const calls = fetchMock.mock.calls as unknown as FetchCall[];
  expect(String(calls[0][0])).toContain("/api/v1/auth/preferences");
  expect(calls[0][1]?.method).toBe("PATCH");
  // Only `rowSpacing` travels: patching one key must not disturb the density.
  expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ rowSpacing: "comfortable" });
});

it("reads and writes the general settings on the app-settings route", async () => {
  const value: ShellGeneralSettings = { titleZh: "捷发", titleEn: "Jiefa", subtitleZh: "", subtitleEn: "", footerHtmlZh: "", footerHtmlEn: "", logoDataUrl: null };
  const fetchMock = respondWith(value);
  vi.stubGlobal("fetch", fetchMock);

  await loadGeneralSettings();
  await saveGeneralSettings(value);

  const calls = fetchMock.mock.calls as unknown as FetchCall[];
  expect(String(calls[0][0])).toContain("/api/v1/app-settings/general");
  expect(calls[0][1]?.method ?? "GET").toBe("GET");
  expect(calls[1][1]?.method).toBe("PUT");
  expect(JSON.parse(String(calls[1][1]?.body))).toEqual(value);
});

/**
 * 「设置 → 通知」的传输口:四个方法一一对应四个端点,两个 `save*` 把整条改动 PATCH 上去。
 *
 * 后两条用例钉的是 409 的**形状**:共享面用 `isNotificationManagedConflict` 判定托管冲突,
 * 它只认带 `status === 409` 或 `code === "notification_group_managed"` 的 rejection。宿主这边
 * 抛的是 `PlatformRequestError`(自带 `status`),所以 adapter 不必再包一层——这两条就是那个
 * "不必"的证据,免得日后有人把错误换成没有 `status` 的类型,而页面悄悄不再回滚。
 */
describe("notificationSettingsAdapter", () => {
  const group = { key: "exam", title: { zh: "考试", en: "Exams" }, description: { zh: "", en: "" }, managed: false, editable: true, scenes: [] };
  const change = { group: "exam", scene: "exam.result_released", channel: "in_app", enabled: true } as const;

  it("maps the four methods onto the notification-settings routes", async () => {
    const fetchMock = respondWith(group);
    vi.stubGlobal("fetch", fetchMock);

    await notificationSettingsAdapter.load();
    await notificationSettingsAdapter.savePreference(change);
    await notificationSettingsAdapter.loadPolicy();
    await notificationSettingsAdapter.savePolicy({ group: "exam", managed: true });

    const calls = fetchMock.mock.calls as unknown as FetchCall[];
    expect(calls.map(([, init]) => init?.method ?? "GET")).toEqual(["GET", "PATCH", "GET", "PATCH"]);
    expect(String(calls[0][0])).toContain("/api/v1/notification-settings");
    expect(String(calls[1][0])).toContain("/api/v1/notification-settings/preferences");
    expect(String(calls[2][0])).toContain("/api/v1/notification-settings/policy");
    expect(String(calls[3][0])).toContain("/api/v1/notification-settings/policy");
    // 两种改动原样送上去:渠道开关是四元组,托管开关是 `{group, managed}`。
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual(change);
    expect(JSON.parse(String(calls[3][1]?.body))).toEqual({ group: "exam", managed: true });
  });

  /**
   * 两次重叠的读必须各发各的:`platformRequest` 会合并在途的纯 GET,而这一页的重读全是
   * 「刚写完,再读一遍」。PATCH 撞上 409 之后的那次恢复性重读一旦并到冲突之前就已在途的
   * GET 上,回来的是一份仍然「未托管」的旧视图,页面会把它当成新事实。
   */
  it("keeps overlapping reads separate so a conflict reload never joins a stale GET", async () => {
    let release: () => void = () => undefined;
    const inFlight = new Promise<void>((resolve) => { release = resolve; });
    const fetchMock = vi.fn(async () => { await inFlight; return new Response(JSON.stringify(group), { status: 200, headers: { "content-type": "application/json" } }); });
    vi.stubGlobal("fetch", fetchMock);

    const first = notificationSettingsAdapter.load();
    const second = notificationSettingsAdapter.load();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    release();
    await Promise.all([first, second]);

    // 退出合并靠的就是这个多出来的 fetch 选项(`sharedGetKey` 只合并「除 method 外别无选项」的 GET)。
    const calls = fetchMock.mock.calls as unknown as FetchCall[];
    expect(calls.map(([, init]) => init?.cache)).toEqual(["no-store", "no-store"]);
    // 同一条路径的另一次读也一样不合并。
    await Promise.all([notificationSettingsAdapter.loadPolicy(), notificationSettingsAdapter.loadPolicy()]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("maps the DingTalk channel methods onto /channels/dingtalk", async () => {
    const channel = { baseUrl: "", appKey: "", baseUrlInherited: false, appKeyInherited: false, hasCredential: false, credentialSource: "none", configured: false, updatedAt: null };
    const fetchMock = respondWith(channel);
    vi.stubGlobal("fetch", fetchMock);

    await notificationSettingsAdapter.loadDingtalkChannel?.();
    await notificationSettingsAdapter.saveDingtalkChannel?.({ baseUrl: "https://auth.example.com", credential: "" });
    await notificationSettingsAdapter.testDingtalkChannel?.();

    const calls = fetchMock.mock.calls as unknown as FetchCall[];
    expect(calls.map(([, init]) => init?.method ?? "GET")).toEqual(["GET", "PUT", "POST"]);
    expect(String(calls[0][0])).toContain("/api/v1/notification-settings/channels/dingtalk");
    expect(calls[0][1]?.cache).toBe("no-store");
    expect(String(calls[1][0])).toContain("/api/v1/notification-settings/channels/dingtalk");
    // 只送改动过的键:`""` 是「清除」,必须原样过线。
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({ baseUrl: "https://auth.example.com", credential: "" });
    expect(String(calls[2][0])).toContain("/api/v1/notification-settings/channels/dingtalk/test");
  });

  it("rejects a managed 409 in the shape the shared surface recognises", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: { code: "notification_group_managed" } }), { status: 409, headers: { "content-type": "application/json" } })));

    const error = await notificationSettingsAdapter.savePreference(change).then(() => null, (reason: unknown) => reason);
    expect(error).not.toBeNull();
    expect((error as { status?: unknown }).status).toBe(409);
    expect(isNotificationManagedConflict(error)).toBe(true);
  });

  it("keeps an unrelated failure out of the managed-conflict path", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "boom" }), { status: 500, headers: { "content-type": "application/json" } })));

    const error = await notificationSettingsAdapter.savePreference(change).then(() => null, (reason: unknown) => reason);
    expect(error).not.toBeNull();
    expect(isNotificationManagedConflict(error)).toBe(false);
  });
});

/**
 * Two-phase load (perceived loading): both requests leave in the same tick and the identity only
 * waits for `/auth/me`, so the page's first list request no longer queues behind `/auth/session`.
 */
describe("startShellIdentityLoad", () => {
  /** `/auth/me` answers immediately; `/auth/session` is controlled per test. */
  function route(session: (init?: RequestInit) => Promise<Response>) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/v1/auth/session")) return session(init);
      return new Response(JSON.stringify({ id: "u1", name: "张三", permissions: ["ops.upstream_health.view"] }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const never = () => new Promise<Response>(() => undefined);

  it("sends both requests in the same tick", () => {
    const fetchMock = route(never);
    void startShellIdentityLoad("Fallback", labels);
    const paths = fetchMock.mock.calls.map(([input]) => String(input));
    expect(paths.some((path) => path.includes("/api/v1/auth/me"))).toBe(true);
    expect(paths.some((path) => path.includes("/api/v1/auth/session"))).toBe(true);
  });

  it("hands over the identity while the session request is still pending", async () => {
    // A `/auth/session` that never lands: the identity must still be available at once.
    route(never);
    const { identity, session } = await startShellIdentityLoad("Fallback", labels);
    expect(identity.name).toBe("张三");
    expect(identity.permissionRequestUrl).toBeNull();
    expect(session).toBeInstanceOf(Promise);
  });

  it("resolves the session promise with the trimmed request url", async () => {
    route(async () => new Response(JSON.stringify({ permissionRequestUrl: "  https://easyauth.test/request  " }), { status: 200, headers: { "content-type": "application/json" } }));
    const { session } = await startShellIdentityLoad("Fallback", labels);
    await expect(session).resolves.toEqual({ permissionRequestUrl: "https://easyauth.test/request", tableDensity: "compact", rowSpacing: "compact" });
    // The thin wrapper keeps the old contract: one complete identity.
    await expect(loadShellIdentity("Fallback", labels).then((value) => value.permissionRequestUrl)).resolves.toBe("https://easyauth.test/request");
  });

  it("carries the account density onto the complete identity", async () => {
    route(async () => new Response(JSON.stringify({ permissionRequestUrl: null, preferences: { tableDensity: "comfortable" } }), { status: 200, headers: { "content-type": "application/json" } }));
    // The identity is released before the session lands, so it still carries the default there.
    const { identity, session } = await startShellIdentityLoad("Fallback", labels);
    expect(identity.tableDensity).toBe("compact");
    await expect(session).resolves.toEqual({ permissionRequestUrl: null, tableDensity: "comfortable", rowSpacing: "compact" });
    await expect(loadShellIdentity("Fallback", labels).then((value) => value.tableDensity)).resolves.toBe("comfortable");
  });

  it("resolves the session promise with the defaults when the endpoint fails, and never rejects", async () => {
    route(async () => new Response("{}", { status: 503 }));
    const { session } = await startShellIdentityLoad("Fallback", labels);
    await expect(session).resolves.toEqual({ permissionRequestUrl: null, tableDensity: "compact", rowSpacing: "compact" });
  });
});
