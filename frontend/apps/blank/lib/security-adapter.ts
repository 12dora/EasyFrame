"use client";
import type { EnterpriseSecurityAdapter } from "@easy-enterprise/ui/enterprise";
import { platformRequest } from "./platform-api";

function request<T>(path: string, body?: unknown, method?: string, preserveSessionOn401 = false): Promise<T> { return platformRequest<T>(path, { method: method ?? (body === undefined ? "GET" : "POST"), body: body === undefined ? undefined : JSON.stringify(body) }, { preserveSessionOn401 }); }
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> { await request<{ ok: boolean }>("/api/v1/users/me/password", { currentPassword, newPassword }, undefined, true); }
export const enterpriseSecurityAdapter: EnterpriseSecurityAdapter = {
  changePassword,
  loadTotpStatus: () => request("/api/v1/users/me/totp/status"),
  beginTotp: () => request("/api/v1/users/me/totp/begin", {}),
  confirmTotp: (code) => request("/api/v1/users/me/totp/confirm", { code }, undefined, true),
  disableTotp: (password, code) => request("/api/v1/users/me/totp/disable", { password, code }, undefined, true),
  loadPasskeys: () => request("/api/v1/users/me/passkeys"),
  beginPasskeyRegistration: () => request("/api/v1/users/me/passkeys/register/begin", {}),
  completePasskeyRegistration: (stateToken, credential, name) => request("/api/v1/users/me/passkeys/register/complete", { stateToken, credential, name }),
  deletePasskey: (id) => request(`/api/v1/users/me/passkeys/${encodeURIComponent(id)}`, undefined, "DELETE"),
};
