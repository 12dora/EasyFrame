"use client";

import type { EnterpriseLoginAdapter, EnterpriseLogoutAdapter, SerializedPublicKeyCredential } from "@easy-enterprise/ui/enterprise";

const TOKEN_KEY = "enterprise-starter-token";
const AUTH_METHOD_KEY = "enterprise-starter-auth-method";
const API_BASE = process.env.NEXT_PUBLIC_ENTERPRISE_API_BASE_URL?.replace(/\/$/, "") ?? "";
const DEMO_AUTH = process.env.NEXT_PUBLIC_ENTERPRISE_DEMO_AUTH === "true";

export class BlankAuthRequestError extends Error {
  constructor(message: string, readonly status: number, readonly detail: unknown) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, credentials: "include", headers: { ...(init?.body ? { "content-type": "application/json" } : {}), ...init?.headers } });
  const body = await response.json().catch(() => null) as { detail?: unknown } | null;
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : `Request failed (${response.status})`;
    throw new BlankAuthRequestError(message, response.status, detail);
  }
  return body as T;
}

export interface BlankLoginResult { accessToken: string; mustChangePassword?: boolean; }
export interface BlankOidcStatus { enabled: boolean; authorizePath: string; }

export async function passwordLogin(values: { username: string; password: string; totpCode?: string }): Promise<BlankLoginResult> {
  if (DEMO_AUTH) { const result = { accessToken: `demo:${values.username}` }; saveToken(result.accessToken); rememberAuthMethod("local"); return result; }
  const result = await request<BlankLoginResult>("/api/v1/auth/login", { method: "POST", body: JSON.stringify(values) });
  saveToken(result.accessToken); rememberAuthMethod("local"); return result;
}
export function loadOidcStatus() { return request<BlankOidcStatus>("/api/v1/auth/oidc/status"); }
export function beginPasskeyLogin(username: string, password: string) { return request<{ options: unknown; stateToken: string }>("/api/v1/auth/login/passkey/begin", { method: "POST", body: JSON.stringify({ username, password }) }); }
export async function completePasskeyLogin(username: string, password: string, stateToken: string, credential: SerializedPublicKeyCredential): Promise<BlankLoginResult> { const result = await request<BlankLoginResult>("/api/v1/auth/login/passkey/complete", { method: "POST", body: JSON.stringify({ username, password, stateToken, credential }) }); saveToken(result.accessToken); rememberAuthMethod("local"); return result; }
export function logout() { localStorage.removeItem(TOKEN_KEY); }
export function authToken() { return typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY); }
export function persistAuthToken(token: string) { saveToken(token); }
export function rememberAuthMethod(method: "local" | "oidc") { localStorage.setItem(AUTH_METHOD_KEY, method); }
export function authMethod(): "local" | "oidc" | null { const value = typeof window === "undefined" ? null : localStorage.getItem(AUTH_METHOD_KEY); return value === "local" || value === "oidc" ? value : null; }
export function clearAuthMethod() { localStorage.removeItem(AUTH_METHOD_KEY); }
export function oidcUrl(authorizePath: string, next: string) { return `${API_BASE}${authorizePath}?next=${encodeURIComponent(next)}`; }
function saveToken(token: string) { localStorage.setItem(TOKEN_KEY, token); }

export const enterpriseLoginAdapter: EnterpriseLoginAdapter = {
  loadOidcStatus,
  passwordLogin,
  beginPasskeyLogin,
  completePasskeyLogin,
  startOidcLogin: (status, target) => window.location.assign(oidcUrl(status.authorizePath, target)),
};

export const enterpriseLogoutAdapter: EnterpriseLogoutAdapter = {
  revoke: () => request("/api/v1/auth/logout", { method: "POST", headers: authToken() ? { Authorization: `Bearer ${authToken()}` } : {} }),
  loadOidcStatus,
  authMethod,
  clearLocalSession: logout,
  clearAuthMethod,
};
