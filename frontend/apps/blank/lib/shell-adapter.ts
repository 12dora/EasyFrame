"use client";

import {
  resolveEnterpriseIdentityLabel,
  type EnterpriseGeneralSettingsAdapter,
  type EnterpriseGeneralSettingsValue,
  type EnterpriseIdentityKind,
  type EnterpriseIdentityLabels,
  type NotificationGroupView,
  type NotificationPolicyChange,
  type NotificationPolicyView,
  type NotificationSettingsAdapter,
  type NotificationSettingsView,
  type NotificationSwitchChange,
} from "@easy-enterprise/ui/enterprise";
import { DEFAULT_ROW_SPACING, type RowSpacing } from "@easy-enterprise/ui";
import { DEFAULT_TABLE_DENSITY, type TableDensity } from "@easy-enterprise/ui/table";
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
  /** EasyAuth permission-request entry point (`/auth/session`); null when unset or unreadable. */
  permissionRequestUrl: string | null;
  /**
   * Account preference: list row density (`/auth/session` -> `preferences.tableDensity`),
   * compact by default. It travels with the account, not with the browser.
   */
  tableDensity: TableDensity;
  /**
   * Account preference: row spacing (`/auth/session` -> `preferences.rowSpacing`), compact by
   * default. Independent of `tableDensity` and carried the same way — on the account, never in
   * the browser.
   */
  rowSpacing: RowSpacing;
}
export interface ShellReminder { id: string; title: string; detail: string; urgent: boolean; }
/** The wire shape of `/api/v1/app-settings/general`; identical to the package value type. */
export type ShellGeneralSettings = EnterpriseGeneralSettingsValue;

/**
 * `GET /api/v1/auth/session`: login-gated only, no permission code.
 *
 * `preferences` carries the look-and-feel settings stored on the account (the table density and
 * the row spacing, two independent steps): kept server-side rather than in the browser, so another
 * machine or another browser still shows what this person picked. A missing field hydrates to the
 * contract default (compact) — that is contract hydration, not a compatibility shim.
 */
interface AuthSession {
  permissionRequestUrl?: string | null;
  preferences?: { tableDensity?: string | null; rowSpacing?: string | null } | null;
}

/** What `/auth/session` lands alongside the identity: the request URL plus the account preferences. */
export interface ShellSession {
  permissionRequestUrl: string | null;
  tableDensity: TableDensity;
  rowSpacing: RowSpacing;
}

/** `/auth/session` wait cap: on timeout treat the request URL as missing. */
export const SESSION_TIMEOUT_MS = 5000;

function trimmed(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

/** Contract hydration: anything but the two known steps (missing included) falls back to the default. */
export function tableDensityOf(value: unknown): TableDensity {
  return value === "comfortable" || value === "compact" ? value : DEFAULT_TABLE_DENSITY;
}

/** Same contract hydration for the sibling preference: anything unknown (missing included) is compact. */
export function rowSpacingOf(value: unknown): RowSpacing {
  return value === "comfortable" || value === "compact" ? value : DEFAULT_ROW_SPACING;
}

function toShellSession(session: AuthSession | null): ShellSession {
  return {
    permissionRequestUrl: trimmed(session?.permissionRequestUrl),
    tableDensity: tableDensityOf(session?.preferences?.tableDensity),
    rowSpacing: rowSpacingOf(session?.preferences?.rowSpacing),
  };
}

/**
 * The login-gated session: permission-request URL + account preferences. Any failure degrades to
 * the defaults (no URL, compact rows): the endpoint may be missing, the network may flake, and a
 * hung request must not pin the shell on the loading skeleton. 401 here must not log the user out
 * (`preserveSessionOn401`); `/auth/me` remains the session authority.
 */
export async function loadAuthSession(): Promise<ShellSession> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SESSION_TIMEOUT_MS);
  try {
    return toShellSession(await platformRequest<AuthSession>("/api/v1/auth/session", { signal: controller.signal }, { preserveSessionOn401: true }));
  } catch {
    return toShellSession(null);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Write the account preference back (`PATCH /api/v1/auth/preferences`).
 *
 * Only the changed key is sent; the response is the updated session and the caller aligns on it
 * (on failure the caller reverts and says so). No browser storage is involved — the preference
 * belongs to the account, so there is exactly one source of truth.
 */
export function saveTableDensity(tableDensity: TableDensity): Promise<ShellSession> {
  return platformRequest<AuthSession>("/api/v1/auth/preferences", {
    method: "PATCH",
    body: JSON.stringify({ tableDensity }),
  }).then(toShellSession);
}

/**
 * Write the row spacing back (`PATCH /api/v1/auth/preferences`) — the sibling of
 * `saveTableDensity`, same route, same response.
 *
 * Only `rowSpacing` travels: patching one key leaves the other preference alone (the backend owns
 * that), so the two steps can never overwrite each other.
 */
export function saveRowSpacing(rowSpacing: RowSpacing): Promise<ShellSession> {
  return platformRequest<AuthSession>("/api/v1/auth/preferences", {
    method: "PATCH",
    body: JSON.stringify({ rowSpacing }),
  }).then(toShellSession);
}

/**
 * Two-phase result of `startShellIdentityLoad`: the identity lands first, the
 * permission-request URL follows.
 */
export interface ShellIdentityLoad {
  /** The `/auth/me` identity. The request URL and both preferences are defaults here — `session` fills them in. */
  readonly identity: ShellIdentity;
  /** The parallel `/auth/session`; resolves to the defaults on failure or timeout and never rejects. */
  readonly session: Promise<ShellSession>;
}

function toShellIdentity(user: CurrentUser, fallbackName: string, identityLabels: EnterpriseIdentityLabels): ShellIdentity {
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
    permissionRequestUrl: null,
    tableDensity: DEFAULT_TABLE_DENSITY,
    rowSpacing: DEFAULT_ROW_SPACING,
  };
}

/**
 * Two-phase shell identity load (perceived loading): both requests leave in the same tick, but
 * only `/auth/me` is awaited.
 *
 * `/auth/me` is the hard dependency (its 401 is still thrown to the caller). `/auth/session` only
 * supplies a permission-request URL: once the identity is in hand the shell must paint, because
 * the page's first batch of data requests all queue behind the shell. Waiting up to
 * `SESSION_TIMEOUT_MS` for an auxiliary request would delay every table by that much.
 */
export async function startShellIdentityLoad(fallbackName: string, identityLabels: EnterpriseIdentityLabels): Promise<ShellIdentityLoad> {
  // Fire first, await second: both requests leave in the same event-loop turn.
  const session = loadAuthSession();
  const user = await platformRequest<CurrentUser>("/api/v1/auth/me");
  return { identity: toShellIdentity(user, fallbackName, identityLabels), session };
}

/**
 * The all-at-once identity (both requests still parallel). The shell uses
 * `startShellIdentityLoad`; this thin wrapper stays for callers that want one complete identity
 * and for the contract tests — same signature, same return type.
 */
export async function loadShellIdentity(fallbackName: string, identityLabels: EnterpriseIdentityLabels): Promise<ShellIdentity> {
  const { identity, session } = await startShellIdentityLoad(fallbackName, identityLabels);
  const settled = await session;
  return { ...identity, permissionRequestUrl: settled.permissionRequestUrl, tableDensity: settled.tableDensity, rowSpacing: settled.rowSpacing };
}
export async function loadNotifications(): Promise<ShellReminder[]> { const result = await platformRequest<NotificationPage>("/api/v1/notifications?limit=100"); return result.items.filter((item) => !item.readAt).map((item) => ({ id: item.id, title: item.title, detail: item.body, urgent: item.level === "error" || item.level === "warning" })); }
export function dismissNotification(id: string) { return platformRequest<{ ok: boolean }>(`/api/v1/notifications/${encodeURIComponent(id)}/read`, { method: "POST" }); }
export function dismissAllNotifications() { return platformRequest<{ updated: number }>("/api/v1/notifications/read-all", { method: "POST" }); }
export function loadGeneralSettings() { return platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general"); }
export function saveGeneralSettings(value: ShellGeneralSettings) { return platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general", { method: "PUT", body: JSON.stringify(value) }); }
/**
 * 「设置 → 通用」与「设置 → 外观」的全局开关(显示页脚)共用这一个 adapter:同一份通用设置、同一个 PUT。
 * 保存成功后由 surface 自己调 `primeEnterpriseGeneralSettings`,顶栏品牌与页脚立即跟随,宿主不用再广播。
 */
export const generalSettingsAdapter: EnterpriseGeneralSettingsAdapter = { load: loadGeneralSettings, save: saveGeneralSettings };

/**
 * 「设置 → 通知」的传输口(契约见 `packages/easy-enterprise/docs/NOTIFICATION-SETTINGS.md`)。
 *
 * 四个方法一一对应 `/api/v1/notification-settings` 的四个端点,走的是与
 * `generalSettingsAdapter` 同一条 `platformRequest`(credentials、Bearer、401 口径都一样)。
 *
 * 两个 `save*` 直接把响应体交回去:后端返回的是**更新后的整个分组**,页面拿它替换乐观值。
 *
 * 409 不做任何包装:`platformRequest` 失败时抛的 `PlatformRequestError` 自带 `status`,
 * 正好命中共享包的 `isNotificationManagedConflict`(它只认 `status === 409` 或
 * `code === "notification_group_managed"`,不 `instanceof` 具体错误类),页面据此回滚并重读。
 *
 * 两个读都带 `cache: "no-store"`,为的是**退出在途 GET 合并**(`sharedGetKey` 只合并「除
 * `method` 外没有任何 fetch 选项」的纯 GET,多一个键就不合并了)。这一页的重读全是
 * 「刚写完,再读一遍」:切页签要重拉,PATCH 撞上 409 之后更要重拉——而 409 恰恰意味着
 * 分组在这段时间里被改成了托管。若这次恢复性重读并到了冲突**之前**就已在途的那一个 GET 上,
 * 回来的是一份仍然「未托管」的旧视图,页面会拿它当新事实画上去,用户看到的就是一张点不动
 * 却不说明原因的表。`no-store` 本身对这几个私有响应也正是想要的语义。
 */
export const notificationSettingsAdapter: NotificationSettingsAdapter = {
  load: () => platformRequest<NotificationSettingsView>("/api/v1/notification-settings", { cache: "no-store" }),
  savePreference: (change: NotificationSwitchChange) =>
    platformRequest<NotificationGroupView>("/api/v1/notification-settings/preferences", { method: "PATCH", body: JSON.stringify(change) }),
  loadPolicy: () => platformRequest<NotificationPolicyView>("/api/v1/notification-settings/policy", { cache: "no-store" }),
  savePolicy: (change: NotificationPolicyChange) =>
    platformRequest<NotificationGroupView>("/api/v1/notification-settings/policy", { method: "PATCH", body: JSON.stringify(change) }),
};
