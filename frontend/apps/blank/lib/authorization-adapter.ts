"use client";

import type {
  EnterpriseAccessSettingsAdapter,
  EnterpriseAuthzStatus,
  EnterpriseConnectionResult,
  EnterpriseIdentityIntegrationSettings,
  EnterpriseManifestOverview,
  EnterprisePermissionCatalogItem,
  EnterprisePermissionSnapshot,
  EnterpriseIdentityDiscoveryResult,
  EnterpriseCurrentGrant,
  EnterpriseDescriptorKey,
} from "@easy-enterprise/ui/enterprise";
import { normalizeEnterpriseManifest } from "@easy-enterprise/ui/enterprise";
import { platformRequest as request } from "./platform-api";

export interface BlankOidcSettings extends EnterpriseIdentityIntegrationSettings {
  authorizationEndpoint: string; tokenEndpoint: string; jwksUri: string; userinfoEndpoint: string;
  scopes: string; redirectBaseUrl: string; frontendBaseUrl: string; serverBaseUrl: string;
  userSyncIntervalMinutes: number;
}
export interface BlankOidcSettingsUpdate {
  enabled: boolean; issuer: string; authorizationEndpoint: string; tokenEndpoint: string; jwksUri: string; userinfoEndpoint: string;
  clientId: string; clientSecret?: string; scopes: string; redirectBaseUrl: string; frontendBaseUrl: string; serverBaseUrl: string;
  authentikApiBaseUrl: string; authentikApiToken?: string; userSyncEnabled: boolean; userSyncIntervalMinutes: number;
}
export interface BlankEasyAuthSettings { configured: boolean; baseUrl: string; appKey: string; authMode: string; hasCredential: boolean; permissionRequestUrl: string; }
export interface BlankEasyAuthSettingsUpdate { baseUrl: string; appKey: string; credential?: string; permissionRequestUrl: string; }

export const authorizationAdapter: EnterpriseAccessSettingsAdapter = {
  easyAuthConnectionEditable: true,
  loadIdentity: () => request<EnterpriseIdentityIntegrationSettings>("/api/v1/identity-integration/settings"),
  loadStatus: () => request<EnterpriseAuthzStatus>("/api/v1/authz-integration/status"),
  testConnection: async () => { const result = await request<EnterpriseConnectionResult & { errorKind?: string | null; errorDetail?: string | null }>("/api/v1/authz-integration/connection-test", { method: "POST", body: "{}" }); return result.error || !result.errorKind ? result : { ...result, error: { kind: result.errorKind, message: result.errorDetail ?? "" } }; },
  loadCatalog: async () => (await request<EnterprisePermissionCatalogItem[]>("/api/v1/authz-integration/permission-catalog")).map((item) => ({ ...item, action: item.action || item.code.split(".").at(-1) || "" })),
  loadSnapshots: () => request<EnterprisePermissionSnapshot[]>("/api/v1/authz-integration/snapshots?limit=100&offset=0&sort=fetched_at_desc"),
  refreshSnapshot: (userId) => request<EnterprisePermissionSnapshot>(`/api/v1/authz-integration/snapshots/${encodeURIComponent(userId)}/refresh`, { method: "POST", body: "{}" }),
  loadManifest: async () => normalizeEnterpriseManifest(await request<EnterpriseManifestOverview>("/api/v1/authz-integration/manifest")),
  loadOidcSettings,
  saveOidcSettings: (value, secrets) => saveOidcSettings({ enabled: value.enabled, issuer: value.issuer, authorizationEndpoint: value.authorizationEndpoint, tokenEndpoint: value.tokenEndpoint, jwksUri: value.jwksUri, userinfoEndpoint: value.userinfoEndpoint, clientId: value.clientId, ...(secrets.clientSecret !== undefined ? { clientSecret: secrets.clientSecret } : {}), scopes: value.scopes, redirectBaseUrl: value.redirectBaseUrl, frontendBaseUrl: value.frontendBaseUrl, serverBaseUrl: value.serverBaseUrl, authentikApiBaseUrl: value.authentikApiBaseUrl, ...(secrets.authentikApiToken !== undefined ? { authentikApiToken: secrets.authentikApiToken } : {}), userSyncEnabled: value.userSyncEnabled, userSyncIntervalMinutes: value.userSyncIntervalMinutes }),
  loadEasyAuthSettings,
  saveEasyAuthSettings: (value, credential) => saveEasyAuthSettings({ baseUrl: value.baseUrl, appKey: value.appKey, ...(credential !== undefined ? { credential } : {}), permissionRequestUrl: value.permissionRequestUrl }),
  testIdentityConnection: async () => { const result = await request<{ ok: boolean; latencyMs: number; errorDetail?: string | null }>("/api/v1/identity-integration/connection-test", { method: "POST", body: "{}" }); return result; },
  discoverIdentity: (issuer) => request<EnterpriseIdentityDiscoveryResult>("/api/v1/identity-integration/discover", { method: "POST", body: JSON.stringify({ issuer: issuer.trim() || null }) }),
  syncIdentityUsers: async () => { const result = await request<{ status: string; summary: string }>("/api/v1/identity-integration/user-sync", { method: "POST", body: "{}" }); return { ok: result.status === "completed", summary: result.summary, ...(result.status === "completed" ? {} : { errorDetail: result.summary }) }; },
  loadMyGrants: async () => (await request<Array<{ permission: string; dataScope: EnterpriseCurrentGrant["dataScope"]; source?: string | null }>>("/api/v1/authz-integration/my-grants")).map((grant) => ({ permissionCode: grant.permission, dataScope: grant.dataScope, source: grant.source })),
  loadDescriptorKeys: () => request<EnterpriseDescriptorKey[]>("/api/v1/authz-integration/descriptor-keys"),
  createDescriptorKey: (name) => request("/api/v1/authz-integration/descriptor-keys", { method: "POST", body: JSON.stringify({ name }) }),
  updateDescriptorKey: (id, active) => request<EnterpriseDescriptorKey>(`/api/v1/authz-integration/descriptor-keys/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ active }) }),
  deleteDescriptorKey: (id) => request(`/api/v1/authz-integration/descriptor-keys/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

export function loadOidcSettings() { return request<BlankOidcSettings>("/api/v1/identity-integration/settings"); }
export function saveOidcSettings(payload: BlankOidcSettingsUpdate) { return request<BlankOidcSettings>("/api/v1/identity-integration/settings", { method: "PUT", body: JSON.stringify(payload) }); }
export function loadEasyAuthSettings() { return request<BlankEasyAuthSettings>("/api/v1/authz-integration/settings"); }
export function saveEasyAuthSettings(payload: BlankEasyAuthSettingsUpdate) { return request<BlankEasyAuthSettings>("/api/v1/authz-integration/settings", { method: "PUT", body: JSON.stringify(payload) }); }
