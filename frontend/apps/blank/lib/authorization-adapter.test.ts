/**
 * Adapter-level pins for the EasyAuth settings wire format.
 * Focus: write-only secrets stay omitted unless the form actually sent them.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { authorizationAdapter } from "./authorization-adapter";

type FetchCall = [input: RequestInfo | URL, init?: RequestInit];

const settings = {
  baseUrl: "https://authz.example.com",
  appKey: "enterprise-blank",
  hasCredential: true,
  hasWebhookSecret: true,
  permissionRequestUrl: "https://authz.example.com/apply",
};

function lastBody(calls: FetchCall[]): Record<string, unknown> {
  const raw = calls[calls.length - 1]?.[1]?.body;
  if (typeof raw !== "string") throw new Error("expected JSON string body");
  return JSON.parse(raw) as Record<string, unknown>;
}

function stubFetch() {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(settings), { status: 200, headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  const store: Record<string, string> = {};
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => { store[key] = value; },
    removeItem: (key: string) => { delete store[key]; },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("saveEasyAuthSettings omits untouched secrets so the backend keeps stored values", async () => {
  const fetchMock = stubFetch();

  await authorizationAdapter.saveEasyAuthSettings(settings, { credential: undefined, webhookSecret: undefined });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]);
  expect(body).toEqual({ baseUrl: settings.baseUrl, appKey: settings.appKey, permissionRequestUrl: settings.permissionRequestUrl });
  expect(Object.prototype.hasOwnProperty.call(body, "credential")).toBe(false);
  expect(Object.prototype.hasOwnProperty.call(body, "webhookSecret")).toBe(false);
});

it("saveEasyAuthSettings sends a typed webhook secret without touching credential", async () => {
  const fetchMock = stubFetch();

  await authorizationAdapter.saveEasyAuthSettings(settings, { webhookSecret: "whsec-live" });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]);
  expect(body).toEqual({ baseUrl: settings.baseUrl, appKey: settings.appKey, webhookSecret: "whsec-live", permissionRequestUrl: settings.permissionRequestUrl });
  expect(Object.prototype.hasOwnProperty.call(body, "credential")).toBe(false);
});

it("saveEasyAuthSettings sends empty strings when the form clears a stored secret", async () => {
  const fetchMock = stubFetch();

  await authorizationAdapter.saveEasyAuthSettings(settings, { credential: "", webhookSecret: "" });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]);
  expect(body.credential).toBe("");
  expect(body.webhookSecret).toBe("");
});

it("saveOidcSettings no longer sends the retired Authentik user-sync fields", async () => {
  const fetchMock = stubFetch();
  const oidc = {
    enabled: true,
    issuer: "https://id.example.com/application/o/blank/",
    authorizationEndpoint: "https://id.example.com/application/o/authorize/",
    tokenEndpoint: "https://id.example.com/application/o/token/",
    jwksUri: "https://id.example.com/application/o/blank/jwks/",
    userinfoEndpoint: "https://id.example.com/application/o/userinfo/",
    clientId: "enterprise-blank",
    hasClientSecret: true,
    scopes: "openid profile email",
    redirectBaseUrl: "http://localhost:8100",
    redirectUri: "http://localhost:8100/api/v1/auth/oidc/callback",
    frontendBaseUrl: "http://localhost:3100",
    serverBaseUrl: "http://localhost:8100",
  };

  await authorizationAdapter.saveOidcSettings(oidc, { clientSecret: "s3cret" });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]);
  expect(body.clientSecret).toBe("s3cret");
  for (const retired of ["authentikApiBaseUrl", "authentikApiToken", "userSyncEnabled", "userSyncIntervalMinutes"]) {
    expect(Object.prototype.hasOwnProperty.call(body, retired)).toBe(false);
  }
});
