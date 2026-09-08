/**
 * Adapter-level pins for the shell identity line and the general-settings wire.
 * Focus: the identity label precedence (superadmin > role groups > any grant >
 * guest) and the `/app-settings/general` read/write shape.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { loadShellIdentity, loadGeneralSettings, saveGeneralSettings, type ShellGeneralSettings } from "./shell-adapter";

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
  expect([identity.name, identity.identity, identity.identityKind]).toEqual(["Fallback", "游客", "guest"]);
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
