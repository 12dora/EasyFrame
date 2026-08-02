"use client";

import { authToken, logout } from "./auth-adapter";

const API_BASE = process.env.NEXT_PUBLIC_ENTERPRISE_API_BASE_URL?.replace(/\/$/, "") ?? "";
export const BLANK_AUTH_INVALIDATED_EVENT = "enterprise-starter:auth-invalidated";

interface PlatformRequestOptions {
  preserveSessionOn401?: boolean;
}

/** Protected platform transport shared by all blank-host adapters. */
export async function platformRequest<T>(path: string, init: RequestInit = {}, options: PlatformRequestOptions = {}): Promise<T> {
  const tokenUsed = authToken();
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
    throw new Error(message);
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
