"use client";

import type {
  CreateLocalAccountInput,
  EnterpriseLocalAccountsAdapter,
  LocalAccountDetail,
  LocalAccountListResult,
  LocalAccountsPermissionCatalogItem,
  ResetLocalAccountPasswordInput,
  SetLocalAccountPermissionsInput,
  UpdateLocalAccountInput,
} from "@easy-enterprise/ui/enterprise-local-accounts";
import { PlatformRequestError, platformRequest } from "./platform-api";

/**
 * Implicit self-service codes for every local account (contract BASELINE_SELF_SERVICE).
 * Host-owned constant so EasyUI stays free of hardcoded permission codes.
 */
export const LOCAL_ACCOUNT_BASELINE_PERMISSIONS = [
  "auth.totp.create",
  "auth.totp.advance",
  "auth.passkey.view",
  "auth.passkey.create",
  "notification.center.view",
] as const;

export { PlatformRequestError };

export const localAccountsAdapter: EnterpriseLocalAccountsAdapter = {
  listAccounts: (params) => {
    const search = params?.search?.trim();
    const query = search ? `?search=${encodeURIComponent(search)}` : "";
    return platformRequest<LocalAccountListResult>(`/api/v1/local-accounts${query}`);
  },
  getAccount: (id) => platformRequest<LocalAccountDetail>(`/api/v1/local-accounts/${encodeURIComponent(id)}`),
  createAccount: (input: CreateLocalAccountInput) =>
    platformRequest<LocalAccountDetail>("/api/v1/local-accounts", {
      method: "POST",
      body: JSON.stringify({
        username: input.username,
        email: input.email ?? null,
        password: input.password,
        mustChangePassword: input.mustChangePassword ?? true,
        isAdmin: input.isAdmin ?? false,
        permissions: input.permissions ?? [],
        ...(input.expiresAt ? { expiresAt: input.expiresAt } : {}),
      }),
    }),
  updateAccount: (id, patch: UpdateLocalAccountInput) =>
    platformRequest<LocalAccountDetail>(`/api/v1/local-accounts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      // Preserve tri-state expiresAt: omit / null / value (JSON.stringify keeps null).
      body: JSON.stringify(patch),
    }),
  deleteAccount: (id) =>
    platformRequest<void>(`/api/v1/local-accounts/${encodeURIComponent(id)}`, { method: "DELETE" }),
  resetPassword: (id, input: ResetLocalAccountPasswordInput) =>
    platformRequest<void>(`/api/v1/local-accounts/${encodeURIComponent(id)}/password`, {
      method: "POST",
      body: JSON.stringify({
        password: input.password,
        mustChangePassword: input.mustChangePassword ?? true,
      }),
    }),
  setPermissions: (id, input: SetLocalAccountPermissionsInput) =>
    platformRequest<LocalAccountDetail>(`/api/v1/local-accounts/${encodeURIComponent(id)}/permissions`, {
      method: "PUT",
      body: JSON.stringify({
        permissions: input.permissions,
        expectedVersion: input.expectedVersion,
      }),
    }),
  disableTotp: (id) =>
    platformRequest<void>(`/api/v1/local-accounts/${encodeURIComponent(id)}/totp`, { method: "DELETE" }),
  loadPermissionCatalog: async () => {
    // Gated on accounts.local.view (not authz.integration.*) so delegated managers can load grants.
    const result = await platformRequest<{ data: LocalAccountsPermissionCatalogItem[] }>(
      "/api/v1/local-accounts/permission-catalog",
    );
    return Array.isArray(result?.data) ? result.data : [];
  },
};
