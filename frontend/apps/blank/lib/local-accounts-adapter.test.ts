/**
 * Adapter-level pins for local-accounts wire format.
 * Focus: expiresAt tri-state on PATCH and no-content DELETE handling.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { localAccountsAdapter } from "./local-accounts-adapter";

type FetchCall = [input: RequestInfo | URL, init?: RequestInit];

function lastBody(calls: FetchCall[]): unknown {
  const init = calls[calls.length - 1]?.[1];
  const raw = init?.body;
  if (typeof raw !== "string") throw new Error("expected JSON string body");
  return JSON.parse(raw);
}

function lastUrl(calls: FetchCall[]): string {
  const input = calls[calls.length - 1]?.[0];
  return String(input);
}

function lastInit(calls: FetchCall[]): RequestInit {
  return calls[calls.length - 1]?.[1] ?? {};
}

beforeEach(() => {
  const store: Record<string, string> = {};
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
    removeItem: (key: string) => {
      delete store[key];
    },
  });
  vi.stubGlobal("window", {
    dispatchEvent: () => true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("updateAccount omits expiresAt when the field is absent (not null)", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ id: "acc-1" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await localAccountsAdapter.updateAccount("acc-1", { email: "ops@example.com" });

  const calls = fetchMock.mock.calls as FetchCall[];
  expect(lastUrl(calls)).toContain("/api/v1/local-accounts/acc-1");
  expect(lastInit(calls).method).toBe("PATCH");
  const body = lastBody(calls) as Record<string, unknown>;
  expect(body).toEqual({ email: "ops@example.com" });
  expect(Object.prototype.hasOwnProperty.call(body, "expiresAt")).toBe(false);
  // Pin exact JSON: omitted field stays absent, not serialized as null.
  expect(JSON.stringify(body)).toBe(JSON.stringify({ email: "ops@example.com" }));
});

it("updateAccount sends expiresAt: null explicitly when clearing expiry", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ id: "acc-1" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await localAccountsAdapter.updateAccount("acc-1", { expiresAt: null });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]) as Record<string, unknown>;
  expect(body).toEqual({ expiresAt: null });
  expect(JSON.stringify(body)).toBe(JSON.stringify({ expiresAt: null }));
});

it("updateAccount sends expiresAt as timezone-aware ISO-8601 instant", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ id: "acc-1" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  // Instant with explicit Z offset — must not be rewritten to a local-naive string.
  const iso = "2027-06-15T12:30:00.000Z";
  await localAccountsAdapter.updateAccount("acc-1", { expiresAt: iso });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]) as Record<string, unknown>;
  expect(body).toEqual({ expiresAt: iso });
  expect(JSON.stringify(body)).toBe(JSON.stringify({ expiresAt: "2027-06-15T12:30:00.000Z" }));
  expect(String(body.expiresAt)).toMatch(/Z$|[+-]\d{2}:\d{2}$/);
  expect(String(body.expiresAt)).not.toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
});

it("deleteAccount tolerates 204 no-content responses", async () => {
  const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(localAccountsAdapter.deleteAccount("acc-1")).resolves.toBeUndefined();
  const calls = fetchMock.mock.calls as FetchCall[];
  expect(lastUrl(calls)).toContain("/api/v1/local-accounts/acc-1");
  expect(lastInit(calls).method).toBe("DELETE");
});

it("createAccount omits expiresAt when not provided", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ id: "acc-new" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  await localAccountsAdapter.createAccount({
    username: "newbie",
    password: "Aa1!aaaaaaaaaa",
  });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]) as Record<string, unknown>;
  expect(Object.prototype.hasOwnProperty.call(body, "expiresAt")).toBe(false);
});

it("createAccount sends expiresAt as the exact ISO instant when provided", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ id: "acc-new" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  const iso = "2027-06-15T12:30:00.000Z";
  await localAccountsAdapter.createAccount({
    username: "newbie",
    password: "Aa1!aaaaaaaaaa",
    expiresAt: iso,
  });

  const body = lastBody(fetchMock.mock.calls as FetchCall[]) as Record<string, unknown>;
  expect(body.expiresAt).toBe(iso);
  expect(JSON.stringify(body)).toContain(`"expiresAt":"${iso}"`);
  expect(String(body.expiresAt)).toMatch(/Z$|[+-]\d{2}:\d{2}$/);
  expect(String(body.expiresAt)).not.toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
});
