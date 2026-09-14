"use client";

import {
  resolveEnterpriseIdentityLabel,
  type EnterpriseGeneralSettingsValue,
  type EnterpriseIdentityKind,
  type EnterpriseIdentityLabels,
} from "@easy-enterprise/ui/enterprise";
import { platformRequest } from "./platform-api";

export type SecurityCapabilities = { passwordChange: boolean; totpStatus: boolean; totpEnroll: boolean; totpDisable: boolean; passkeyList: boolean; passkeyRegister: boolean; passkeyDelete: boolean; };
interface CurrentUser {
  id: string;
  name: string;
  email?: string | null;
  avatarUrl?: string | null;
  hasLocalPassword?: boolean;
  mustChangePassword?: boolean;
  permissions?: string[];
  roleGroups?: string[] | null;
  securityCapabilities?: Partial<SecurityCapabilities> | null;
  /** Server-derived local account id (04 §3 /auth/me extension). */
  accountId?: string | null;
  /** Server-derived superadmin predicate — do not infer from permissions. */
  isLocalSuperadmin?: boolean;
}
interface NotificationItem { id: string; title: string; body: string; level: "info" | "success" | "warning" | "error"; createdAt: string; readAt: string | null; href?: string | null; }
interface NotificationPage { items: NotificationItem[]; unreadCount: number; nextCursor: string | null; }
export interface ShellIdentity {
  name: string;
  identity: string;
  avatarUrl: string | null;
  /** Login email; null when absent (onboarding secondary line). */
  email: string | null;
  hasLocalPassword: boolean;
  mustChangePassword: boolean;
  permissions: ReadonlySet<string>;
  securityCapabilities: SecurityCapabilities;
  /** From `/auth/me.accountId` — used for self-row lockout on local-accounts surface. */
  accountId: string;
  /** From `/auth/me.isLocalSuperadmin` — capability controls, never inferred. */
  isLocalSuperadmin: boolean;
  /** Standing behind `identity`; see `resolveEnterpriseIdentityLabel`. */
  identityKind: EnterpriseIdentityKind;
}
export interface ShellReminder { id: string; title: string; detail: string; urgent: boolean; }
/** The wire shape of `/api/v1/app-settings/general`; identical to the package value type. */
export type ShellGeneralSettings = EnterpriseGeneralSettingsValue;

/** `GET /api/v1/auth/session`: login-gated only, no permission code. */
interface AuthSession { permissionRequestUrl?: string | null }

/** `/auth/session` wait cap: on timeout treat the request URL as missing. */
export const SESSION_TIMEOUT_MS = 5000;

function trimmed(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

/**
 * Permission-request URL. Any failure is null: the endpoint may be missing,
 * the network may flake, and a hung request must not pin the shell on the
 * loading skeleton. 401 here must not log the user out (`preserveSessionOn401`);
 * `/auth/me` remains the session authority.
 */
export async function loadAuthSession(): Promise<string | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SESSION_TIMEOUT_MS);
  try {
    const session = await platformRequest<AuthSession>("/api/v1/auth/session", { signal: controller.signal }, { preserveSessionOn401: true });
    return trimmed(session.permissionRequestUrl);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function loadShellIdentity(fallbackName: string, identityLabels: EnterpriseIdentityLabels): Promise<ShellIdentity> {
  const user = await platformRequest<CurrentUser>("/api/v1/auth/me");
  const permissions = new Set(user.permissions ?? []);
  const capability = (key: keyof SecurityCapabilities) => user.securityCapabilities?.[key] === true;
  const isLocalSuperadmin = user.isLocalSuperadmin === true;
  const resolvedIdentity = resolveEnterpriseIdentityLabel({ isLocalSuperadmin, roleGroups: user.roleGroups, permissions }, identityLabels);
  const email = trimmed(user.email);
  return {
    name: trimmed(user.name) || email || fallbackName,
    identity: resolvedIdentity.label,
    identityKind: resolvedIdentity.kind,
    email,
    avatarUrl: user.avatarUrl ?? null,
    hasLocalPassword: user.hasLocalPassword === true,
    mustChangePassword: user.mustChangePassword === true,
    permissions,
    securityCapabilities: {
      passwordChange: capability("passwordChange"),
      totpStatus: capability("totpStatus"),
      totpEnroll: capability("totpEnroll"),
      totpDisable: capability("totpDisable"),
      passkeyList: capability("passkeyList"),
      passkeyRegister: capability("passkeyRegister"),
      passkeyDelete: capability("passkeyDelete"),
    },
    accountId: typeof user.accountId === "string" ? user.accountId : user.id,
    isLocalSuperadmin,
  };
}
export async function loadNotifications(): Promise<ShellReminder[]> { const result = await platformRequest<NotificationPage>("/api/v1/notifications?limit=100"); return result.items.filter((item) => !item.readAt).map((item) => ({ id: item.id, title: item.title, detail: item.body, urgent: item.level === "error" || item.level === "warning" })); }
export function dismissNotification(id: string) { return platformRequest<{ ok: boolean }>(`/api/v1/notifications/${encodeURIComponent(id)}/read`, { method: "POST" }); }
export function dismissAllNotifications() { return platformRequest<{ updated: number }>("/api/v1/notifications/read-all", { method: "POST" }); }
export function loadGeneralSettings() { return platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general"); }
export function saveGeneralSettings(value: ShellGeneralSettings) { return platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general", { method: "PUT", body: JSON.stringify(value) }); }
