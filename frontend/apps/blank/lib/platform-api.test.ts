import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlatformRequestError, platformRequest } from "./platform-api";

/**
 * 在途 GET 去重:切页时外壳、页面与 StrictMode 常常同时要同一份数据,一次往返就够了。
 * 写请求、带 signal 的请求、换了 token 或 401 口径的请求一律各走各的。
 */

const token = vi.hoisted(() => ({ value: null as string | null }));
vi.mock("./auth-adapter", () => ({ authToken: () => token.value, logout: vi.fn() }));

const fetchMock = vi.fn();

beforeEach(() => {
  token.value = null;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 可手动放行的响应;每次 fetch 都拿一份新的 body 对象。 */
function pendingResponses() {
  const releases: (() => void)[] = [];
  fetchMock.mockImplementation(() => new Promise((resolve) => {
    releases.push(() => resolve({ ok: true, status: 200, json: async () => ({ items: [{ id: "n1" }] }) }));
  }));
  return () => { for (const release of releases.splice(0)) release(); };
}

describe("platformRequest in-flight GET dedupe", () => {
  it("shares one fetch between identical concurrent GETs and hands each caller its own copy", async () => {
    const release = pendingResponses();
    const first = platformRequest<{ items: { id: string }[] }>("/api/v1/notifications?limit=100");
    const second = platformRequest<{ items: { id: string }[] }>("/api/v1/notifications?limit=100", { method: "GET" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    const [a, b] = await Promise.all([first, second]);
    expect(a).toEqual({ items: [{ id: "n1" }] });
    expect(b).toEqual(a);
    expect(b).not.toBe(a);
    a.items.push({ id: "mutated" });
    expect(b.items).toHaveLength(1);
  });

  it("fetches again once the previous request has settled", async () => {
    const release = pendingResponses();
    const first = platformRequest("/api/v1/notifications");
    release();
    await first;
    const second = platformRequest("/api/v1/notifications");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    release();
    await second;
  });

  it("shares a failure with every joined caller", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: "不存在" }) });
    const results = await Promise.all([
      platformRequest("/api/v1/orders/x").catch((cause: unknown) => cause),
      platformRequest("/api/v1/orders/x").catch((cause: unknown) => cause),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    for (const result of results) {
      expect(result).toBeInstanceOf(PlatformRequestError);
      expect((result as PlatformRequestError).status).toBe(404);
    }
  });

  it("never merges writes, requests with a signal or headers, different paths or 401 policies", async () => {
    const release = pendingResponses();
    const calls = [
      platformRequest("/api/v1/orders", { method: "POST", body: "{}" }),
      platformRequest("/api/v1/orders", { method: "POST", body: "{}" }),
      platformRequest("/api/v1/orders", { signal: new AbortController().signal }),
      platformRequest("/api/v1/orders", { signal: new AbortController().signal }),
      platformRequest("/api/v1/orders", { headers: { "x-trace": "1" } }),
      platformRequest("/api/v1/orders?page=2"),
      platformRequest("/api/v1/orders?page=2", {}, { preserveSessionOn401: true }),
    ];
    expect(fetchMock).toHaveBeenCalledTimes(7);
    release();
    await Promise.all(calls);
  });

  it("does not merge across tokens", async () => {
    const release = pendingResponses();
    token.value = "token-a";
    const first = platformRequest("/api/v1/auth/session");
    token.value = "token-b";
    const second = platformRequest("/api/v1/auth/session");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([, init]) => (init as RequestInit & { headers: Record<string, string> }).headers.Authorization)).toEqual(["Bearer token-a", "Bearer token-b"]);
    release();
    await Promise.all([first, second]);
  });

  it("keeps a 204 empty and passes non-GET requests straight through", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204, json: async () => { throw new SyntaxError("empty"); } });
    await expect(platformRequest("/api/v1/orders/o1", { method: "DELETE" })).resolves.toBeUndefined();
  });
});
