"use client";

import { authToken, logout } from "./auth-adapter";

const API_BASE = process.env.NEXT_PUBLIC_ENTERPRISE_API_BASE_URL?.replace(/\/$/, "") ?? "";
export const BLANK_AUTH_INVALIDATED_EVENT = "enterprise-starter:auth-invalidated";

interface PlatformRequestOptions {
  preserveSessionOn401?: boolean;
}

/** Thrown on non-2xx platform responses so surfaces can branch on HTTP status (e.g. 409 CAS). */
export class PlatformRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "PlatformRequestError";
  }
}

/**
 * Protected platform transport shared by all blank-host adapters.
 *
 * 在途表的查找与登记都在第一个 await 之前同步完成,同一事件循环里的并发调用一定能合并上。
 */
export async function platformRequest<T>(path: string, init: RequestInit = {}, options: PlatformRequestOptions = {}): Promise<T> {
  const tokenUsed = authToken();
  const key = sharedGetKey(path, init, options, tokenUsed);
  if (key === null) return sendRequest<T>(path, init, options, tokenUsed);
  let entry = inFlightGets.get(key);
  if (entry) entry.shared = true;
  else {
    const created: InFlightGet = { shared: false, response: sendRequest<unknown>(path, init, options, tokenUsed).finally(() => inFlightGets.delete(key)) };
    inFlightGets.set(key, created);
    entry = created;
  }
  const joined = entry;
  // 被合并过的结果每个调用方各拿一份深拷贝:谁改了自己那份都不会串到别人。
  return joined.response.then((value) => (joined.shared ? cloneBody(value) : value) as T);
}

/**
 * 在途 GET 去重(切页时外壳、页面与 StrictMode 常常同时要同一份数据)。
 *
 * 只合并「纯 GET」:除 `method` 外没有任何 fetch 选项(body、signal、自定义头……)——带 signal 的请求
 * 各自有超时,合并后一个调用方的 abort 会连累别人。键里带上 token 与 401 口径,换人或口径不同都不合并。
 */
interface InFlightGet { shared: boolean; readonly response: Promise<unknown> }
const inFlightGets = new Map<string, InFlightGet>();

function sharedGetKey(path: string, init: RequestInit, options: PlatformRequestOptions, tokenUsed: string | null): string | null {
  const extraKeys = Object.keys(init).filter((name) => name !== "method");
  if (extraKeys.length > 0 || (init.method ?? "GET").toUpperCase() !== "GET") return null;
  return JSON.stringify([path, tokenUsed, options.preserveSessionOn401 === true]);
}

function cloneBody(value: unknown): unknown {
  return value === undefined || typeof structuredClone !== "function" ? value : structuredClone(value);
}

async function sendRequest<T>(path: string, init: RequestInit, options: PlatformRequestOptions, tokenUsed: string | null): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(tokenUsed ? { Authorization: `Bearer ${tokenUsed}` } : {}),
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    invalidateSession(response.status, tokenUsed, options.preserveSessionOn401 === true);
    const body = await response.json().catch(() => null) as { detail?: unknown } | null;
    const message = typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`;
    throw new PlatformRequestError(message, response.status, body?.detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function invalidateSession(status: number, tokenUsed: string | null, preserve: boolean) {
  if (preserve || status !== 401 || typeof window === "undefined") return;
  const currentToken = authToken();
  if (tokenUsed && currentToken !== tokenUsed) return;
  if (!tokenUsed && currentToken) return;
  if (tokenUsed) logout();
  window.dispatchEvent(new Event(BLANK_AUTH_INVALIDATED_EVENT));
}
