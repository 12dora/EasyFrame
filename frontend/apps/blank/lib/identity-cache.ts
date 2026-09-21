"use client";

import { clearAsyncDataCache } from "./async-data-cache";
import { DEFAULT_ROW_SPACING, type RowSpacing } from "@easy-enterprise/ui";
import { DEFAULT_TABLE_DENSITY, type TableDensity } from "@easy-enterprise/ui/table";
import type { SecurityCapabilities, ShellIdentity } from "./shell-adapter";

/**
 * Per-tab shell identity snapshot (perceived loading).
 *
 * Until `/auth/me` answers the shell can only paint a skeleton, so the page below it has not
 * even issued its first list request. Within one tab the second page is almost always the same
 * person: paint the shell and the page from the snapshot first, then reconcile against the real
 * identity — same `accountId` updates in place, a different one replaces the whole thing (the
 * caller repaints).
 *
 * The snapshot only drives pixels: authorization is decided server-side, so a stale snapshot at
 * worst shows one extra sidebar entry for one reconcile. It is written to sessionStorage only
 * (dies with the tab) and never to localStorage — an identity must not be reused across tabs or
 * across sessions.
 *
 * This module is also the external store behind the shell identity (`useSyncExternalStore`): the
 * in-memory copy is the current identity, sessionStorage is only the draft left for the next page
 * load. The server snapshot is always null, so SSR and the first client frame still render the
 * skeleton and React repaints from the real snapshot after hydration — no mismatch, and no
 * `setState` inside an effect.
 *
 * Host adoption: copy this file verbatim, change `CACHE_KEY` to your app's prefix. See
 * `docs/SHELL_PERCEIVED_LOADING.md`.
 */

const CACHE_KEY = "blank.shell.identity";
/** Structure version: bump it whenever `ShellIdentity` changes so old snapshots are discarded whole. */
const CACHE_VERSION = 2;

const CAPABILITY_KEYS = [
  "passwordChange",
  "totpStatus",
  "totpEnroll",
  "totpDisable",
  "passkeyList",
  "passkeyRegister",
  "passkeyDelete",
] as const;

/** The current identity for this page load. A forced-password-change identity lives only here. */
let memory: { locale: string; identity: ShellIdentity } | null = null;
const listeners = new Set<() => void>();
let sessionGeneration = 0;

/** Invalidates old preference requests, even when the same account logs in again. */
export function identitySessionGeneration(): number { return sessionGeneration; }

/** The subscribe half of `useSyncExternalStore`. */
export function subscribeCachedIdentity(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function emit(): void {
  for (const listener of [...listeners]) listener();
}

/** sessionStorage throws in private mode / with storage disabled; reads and writes both shrug it off. */
function readRaw(): string | null {
  try {
    return window.sessionStorage.getItem(CACHE_KEY);
  } catch {
    return null;
  }
}

function writeRaw(value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(CACHE_KEY);
    else window.sessionStorage.setItem(CACHE_KEY, value);
  } catch {
    // Nothing persisted: the memory copy still works, we just leave no draft for the next load.
  }
}

/**
 * Drop a snapshot that failed to parse.
 *
 * This runs inside `getSnapshot` (`readCachedIdentity`), so it must **not** emit: notifying
 * subscribers mid-render is forbidden by `useSyncExternalStore` (React treats it as "the value
 * changed while reading" and retries). Clearing storage is enough — this frame returns null, the
 * screen stays on the skeleton, and the pending `/auth/me` writes the real identity back and
 * notifies normally.
 */
function discardSnapshot(): void {
  memory = null;
  writeRaw(null);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function flag(value: unknown): boolean {
  return value === true;
}

/** Only the two known steps count; a snapshot without the field falls back to the global default. */
function density(value: unknown): TableDensity {
  return value === "comfortable" || value === "compact" ? value : DEFAULT_TABLE_DENSITY;
}

/** The row-spacing twin of `density`; an older snapshot without the field reads as the default. */
function spacing(value: unknown): RowSpacing {
  return value === "comfortable" || value === "compact" ? value : DEFAULT_ROW_SPACING;
}

function toCapabilities(value: unknown): SecurityCapabilities {
  const source = isRecord(value) ? value : {};
  const entries = CAPABILITY_KEYS.map((key) => [key, flag(source[key])] as const);
  return Object.fromEntries(entries) as SecurityCapabilities;
}

/** Deserialize: any key field off-contract voids the whole snapshot — never assemble half an identity. */
function toIdentity(value: unknown): ShellIdentity | null {
  if (!isRecord(value)) return null;
  const accountId = text(value.accountId);
  const name = text(value.name);
  const kind = value.identityKind;
  const permissions = value.permissions;
  if (!accountId || !name) return null;
  if (kind !== "admin" && kind !== "user" && kind !== "guest") return null;
  if (!Array.isArray(permissions) || permissions.some((code) => typeof code !== "string")) return null;
  return {
    name,
    identity: text(value.identity),
    identityKind: kind,
    email: nullableText(value.email),
    avatarUrl: nullableText(value.avatarUrl),
    hasLocalPassword: flag(value.hasLocalPassword),
    // The forced-password-change gate is always answered live by `/auth/me` (and never persisted).
    mustChangePassword: false,
    permissions: new Set(permissions as string[]),
    securityCapabilities: toCapabilities(value.securityCapabilities),
    accountId,
    isLocalSuperadmin: flag(value.isLocalSuperadmin),
    permissionRequestUrl: nullableText(value.permissionRequestUrl),
    tableDensity: density(value.tableDensity),
    rowSpacing: spacing(value.rowSpacing),
  };
}

/**
 * This tab's previous identity. Missing, corrupted, or written under another locale all return
 * null: the identity line and the fallback name are localized copy, so the snapshot from another
 * language is simply wrong — better to wait for `/auth/me` once.
 */
export function readCachedIdentity(locale: string): ShellIdentity | null {
  if (typeof window === "undefined") return null;
  if (memory?.locale === locale) return memory.identity;
  const raw = readRaw();
  if (!raw) return null;
  let record: unknown;
  try {
    record = JSON.parse(raw);
  } catch {
    discardSnapshot();
    return null;
  }
  if (!isRecord(record) || record.v !== CACHE_VERSION || record.locale !== locale) return null;
  const identity = toIdentity(record.identity);
  if (!identity) {
    discardSnapshot();
    return null;
  }
  memory = { locale, identity };
  return identity;
}

/**
 * Install the current identity and notify subscribers. A forced-password-change account is never
 * persisted: that gate must be answered live by `/auth/me` on the next load.
 */
export function writeCachedIdentity(locale: string, identity: ShellIdentity): void {
  // Another account: the previous person's read cache (`useAsyncData`) goes with it, and mounted pages refetch.
  if (memory && memory.identity.accountId !== identity.accountId) {
    sessionGeneration += 1;
    clearAsyncDataCache();
  }
  memory = { locale, identity };
  if (typeof window === "undefined") return;
  writeRaw(identity.mustChangePassword ? null : JSON.stringify({ v: CACHE_VERSION, locale, identity: { ...identity, permissions: [...identity.permissions] } }));
  emit();
}

/**
 * Drop the snapshot when this session ends: the next person must never see the previous one's shell.
 *
 * Call sites are the explicit ones only — the logout adapter's `clearLocalSession`
 * (`endLocalSession`), the password-change success path, and the shell's own eject-to-login.
 * Never from `logout()` itself: the 401 path calls `logout()` too, and there the shell wants to
 * stay on screen while the session is re-checked.
 *
 * The read cache (`useAsyncData`) is dropped too, with `refetch: false`: the page is about to leave,
 * and refetching now would only hit a 401 with no credential.
 */
export function clearCachedIdentity(): void {
  sessionGeneration += 1;
  memory = null;
  clearAsyncDataCache({ refetch: false });
  if (typeof window !== "undefined") writeRaw(null);
  emit();
}

/** Patch only the completed preference into the current session, never the request's old snapshot. */
export function patchCachedPreference(
  locale: string, accountId: string, generation: number,
  key: "tableDensity" | "rowSpacing", value: TableDensity | RowSpacing,
): void {
  if (generation !== sessionGeneration || memory?.locale !== locale || memory.identity.accountId !== accountId) return;
  writeCachedIdentity(locale, { ...memory.identity, [key]: value });
}

function sameStringSet(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  if (left.size !== right.size) return false;
  for (const value of left) if (!right.has(value)) return false;
  return true;
}

/** Whether two identities are indistinguishable on screen — the repaint predicate after a reconcile. */
export function sameIdentity(left: ShellIdentity, right: ShellIdentity): boolean {
  const scalarKeys = ["name", "identity", "identityKind", "email", "avatarUrl", "hasLocalPassword", "mustChangePassword", "accountId", "isLocalSuperadmin", "permissionRequestUrl", "tableDensity", "rowSpacing"] as const;
  if (scalarKeys.some((key) => left[key] !== right[key])) return false;
  if (CAPABILITY_KEYS.some((key) => left.securityCapabilities[key] !== right.securityCapabilities[key])) return false;
  return sameStringSet(left.permissions, right.permissions);
}

/**
 * Snapshot × live identity. Another account (or no snapshot at all) replaces everything; the same
 * account takes the live identity but keeps the snapshot's permission-request URL while
 * `/auth/session` is still in flight, so the onboarding button does not disappear and come back.
 *
 * `sessionSettled` turns that carry-over off: once `/auth/session` has answered, its answer is
 * authoritative even when it is null. Without it a URL that has since been un-configured would
 * survive every same-tab reload, because the snapshot keeps re-supplying it.
 *
 * Both account preferences (the table density and the row spacing) come from `/auth/session` too,
 * so they follow the same rule: keep the snapshot's step until the session lands, otherwise
 * someone who picked "comfortable" would see one compact frame on every reload before it snapped
 * back.
 */
export function reconcileIdentity(cached: ShellIdentity | null, fresh: ShellIdentity, sessionSettled = false): ShellIdentity {
  if (!cached || cached.accountId !== fresh.accountId) return fresh;
  if (sessionSettled) return fresh;
  const tableDensity = cached.tableDensity;
  const rowSpacing = cached.rowSpacing;
  if (fresh.permissionRequestUrl !== null || cached.permissionRequestUrl === null) return { ...fresh, tableDensity, rowSpacing };
  return { ...fresh, permissionRequestUrl: cached.permissionRequestUrl, tableDensity, rowSpacing };
}

/** First-paint yield window: run when the browser goes idle, or at this deadline at the latest. */
export const IDLE_TIMEOUT_MS = 1200;

/**
 * Defer work that "can happen a little later" until the browser is idle (or the deadline, when
 * `requestIdleCallback` is missing — Safari).
 *
 * Post-navigation identity re-checks and silent OIDC re-checks are exactly this kind of work: they
 * do not affect the current frame, yet they compete with the page's first batch of data requests
 * for the backend and the connection pool. Returns a cancel function.
 */
export function scheduleWhenIdle(run: () => void): () => void {
  const idle = typeof window !== "undefined" && typeof window.requestIdleCallback === "function" ? window.requestIdleCallback.bind(window) : null;
  if (!idle) {
    const timer = window.setTimeout(run, IDLE_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }
  const handle = idle(run, { timeout: IDLE_TIMEOUT_MS });
  return () => window.cancelIdleCallback?.(handle);
}
