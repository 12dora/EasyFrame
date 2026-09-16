/**
 * Adapter-level pins for the shell identity line and the general-settings wire.
 * Focus: the identity label precedence (superadmin > role groups > any grant >
 * guest) and the `/app-settings/general` read/write shape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadAuthSession, loadShellIdentity, loadGeneralSettings, saveGeneralSettings, startShellIdentityLoad, type ShellGeneralSettings } from "./shell-adapter";

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
  expect(await loadAuthSession()).toBe("https://easyauth.example.test/request");
  expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/v1/auth/session");
});

it("treats a missing or failed session URL as null without logging out", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ permissionRequestUrl: "  " }), { status: 200, headers: { "content-type": "application/json" } })));
  expect(await loadAuthSession()).toBeNull();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
  expect(await loadAuthSession()).toBeNull();
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
    await expect(session).resolves.toBe("https://easyauth.test/request");
    // The thin wrapper keeps the old contract: one complete identity.
    await expect(loadShellIdentity("Fallback", labels).then((value) => value.permissionRequestUrl)).resolves.toBe("https://easyauth.test/request");
  });

  it("resolves the session promise with null when the endpoint fails, and never rejects", async () => {
    route(async () => new Response("{}", { status: 503 }));
    const { session } = await startShellIdentityLoad("Fallback", labels);
    await expect(session).resolves.toBeNull();
  });
});
