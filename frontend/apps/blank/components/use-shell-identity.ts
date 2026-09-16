"use client";

import type { EnterpriseIdentityLabels } from "@easy-enterprise/ui/enterprise";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AUTH_TOKEN_STORAGE_KEY, endLocalSession } from "../lib/auth-adapter";
import { clearCachedIdentity, readCachedIdentity, reconcileIdentity, sameIdentity, scheduleWhenIdle, subscribeCachedIdentity, writeCachedIdentity } from "../lib/identity-cache";
import type { Locale } from "../lib/messages";
import { BLANK_AUTH_INVALIDATED_EVENT } from "../lib/platform-api";
import { startShellIdentityLoad, type ShellIdentity } from "../lib/shell-adapter";

export interface ShellIdentityState {
  /** null while the identity is unknown, and while the forced-password-change gate is holding. */
  identity: ShellIdentity | null;
  /** `/auth/session` is still in flight. The identity is already usable; only the onboarding page waits. */
  permissionUrlPending: boolean;
  /**
   * Explicit re-fetch (the onboarding page's "check again"); always immediate.
   * Resolves when that re-fetch settles, so `EnterprisePermissionOnboarding` can show its
   * busy state for the real duration.
   */
  refreshIdentity: () => Promise<void>;
}

export interface ShellIdentityParams {
  locale: Locale;
  pathname: string;
  /** Brand fallback used when `/auth/me` has neither a name nor an email. */
  fallbackName: string;
  /** Identity-line labels + role-group separator; see `resolveEnterpriseIdentityLabel`. */
  identityLabels: EnterpriseIdentityLabels;
}

/**
 * The identity state is `lib/identity-cache.ts`, an external store read through
 * `useSyncExternalStore`.
 *
 * The server snapshot is always null, so SSR and the first hydration frame still render the
 * skeleton; the moment hydration completes React repaints from this tab's snapshot and neither
 * the shell nor the page has to wait for `/auth/me`. The reconcile rule lives in the store: the
 * same account updates in place, a different one replaces everything.
 */
function useCachedIdentity(locale: Locale): { identity: ShellIdentity | null; applyIdentity: (fresh: ShellIdentity, sessionSettled?: boolean) => void } {
  const identity = useSyncExternalStore(subscribeCachedIdentity, () => readCachedIdentity(locale), () => null);
  const applyIdentity = useCallback((fresh: ShellIdentity, sessionSettled = false) => {
    const current = readCachedIdentity(locale);
    const next = reconcileIdentity(current, fresh, sessionSettled);
    // Equivalent to what is already painted: do nothing. A routine post-navigation re-check must
    // not re-render the whole tree.
    if (current && sameIdentity(current, next)) return;
    writeCachedIdentity(locale, next);
  }, [locale]);
  return { identity, applyIdentity };
}

interface IdentityLoaderParams extends ShellIdentityParams {
  refreshTick: number;
  applyIdentity: (fresh: ShellIdentity, sessionSettled?: boolean) => void;
  /** `/auth/session` settled (URL, failure or timeout all count). */
  onSessionSettled: () => void;
  /** `/auth/me` failed. */
  onFailure: (cause: unknown) => void;
}

/**
 * Identity loading: `/auth/me` and `/auth/session` leave together, only the former is awaited.
 *
 * The identity is handed to the caller as soon as it lands and `/auth/session` is patched in
 * behind it — even at its 5s cap it no longer pins the whole page on a skeleton, so the page's
 * first list request starts a whole `/auth/session` earlier.
 *
 * First paint and explicit refreshes run immediately; the routine post-navigation re-check is
 * deferred to the idle window so it does not compete with the new page's data requests.
 */
function useIdentityLoader(params: IdentityLoaderParams): void {
  const latest = useRef(params);
  useEffect(() => { latest.current = params; });
  const { locale, pathname, refreshTick } = params;
  const startedRef = useRef(false);
  const tickRef = useRef(refreshTick);
  useEffect(() => {
    let alive = true;
    const run = () => {
      const { fallbackName, identityLabels, applyIdentity, onSessionSettled, onFailure } = latest.current;
      startShellIdentityLoad(fallbackName, identityLabels)
        .then(({ identity, session }) => {
          if (!alive) return;
          applyIdentity(identity);
          void session.then((permissionRequestUrl) => {
            if (!alive) return;
            onSessionSettled();
            // Authoritative, null included: a URL that was un-configured must not keep coming
            // back out of the snapshot on every same-tab reload.
            applyIdentity({ ...identity, permissionRequestUrl }, true);
          });
        })
        .catch((cause: unknown) => { if (alive) onFailure(cause); });
    };
    // Yielding only makes sense when something is already on screen. With no readable snapshot
    // (first paint, or right after a locale switch — snapshots are keyed by locale) the screen is
    // a skeleton, and yielding 1.2s would only keep it blank for longer.
    const immediate = !startedRef.current || tickRef.current !== refreshTick || readCachedIdentity(locale) === null;
    startedRef.current = true;
    tickRef.current = refreshTick;
    if (immediate) { run(); return () => { alive = false; }; }
    const cancel = scheduleWhenIdle(run);
    return () => { alive = false; cancel(); };
  }, [locale, pathname, refreshTick]);
}

/**
 * `/auth/me` loading + the forced-password-change gate; any failure ejects to the login page.
 *
 * Production identity facts come from the trusted gateway / Authentik and are verified by
 * `/auth/me`; the local JWT is only an optional dev/demo credential and is never the gate for
 * entering the shell.
 *
 * The returned `refreshIdentity` is for the zero-grant onboarding page: once the portal approves
 * a grant, one re-fetch lets the user through.
 */
export function useShellIdentity({ locale, pathname, fallbackName, identityLabels }: ShellIdentityParams): ShellIdentityState {
  const router = useRouter();
  const { identity, applyIdentity } = useCachedIdentity(locale);
  const [permissionUrlPending, setPermissionUrlPending] = useState(true);
  // Incrementing re-runs the load; the identity is not cleared, so the page keeps its pixels.
  const [refreshTick, setRefreshTick] = useState(0);
  // Callers awaiting the in-flight explicit refresh. Concurrent calls chain onto one resolution.
  const pendingRefresh = useRef<(() => void) | null>(null);
  const refreshIdentity = useCallback(() => {
    setRefreshTick((tick) => tick + 1);
    return new Promise<void>((resolve) => {
      const previous = pendingRefresh.current;
      pendingRefresh.current = () => { previous?.(); resolve(); };
    });
  }, []);
  const settleRefresh = useCallback(() => {
    const done = pendingRefresh.current;
    pendingRefresh.current = null;
    done?.();
  }, []);
  const ejectedRef = useRef(false);
  // The event listeners need the latest pathname without being rebuilt: carry it in a ref.
  const pathnameRef = useRef(pathname);
  useEffect(() => { pathnameRef.current = pathname; });

  const ejectToLogin = useCallback(() => {
    if (ejectedRef.current) return;
    ejectedRef.current = true;
    // Explicit end of session: the snapshot goes with the credential (see `endLocalSession`).
    endLocalSession();
    router.replace(`/${locale}/login?next=${encodeURIComponent(pathnameRef.current)}`);
  }, [locale, router]);

  // A usable identity releases the eject latch, so a later real failure can eject again.
  const acceptIdentity = useCallback((fresh: ShellIdentity, sessionSettled?: boolean) => { ejectedRef.current = false; applyIdentity(fresh, sessionSettled); settleRefresh(); }, [applyIdentity, settleRefresh]);

  useIdentityLoader({
    fallbackName,
    identityLabels,
    locale,
    pathname,
    refreshTick,
    applyIdentity: acceptIdentity,
    onSessionSettled: () => setPermissionUrlPending(false),
    onFailure: () => { setPermissionUrlPending(false); settleRefresh(); ejectToLogin(); },
  });

  // Forced password change: outside the password page the identity is withheld (the caller keeps
  // painting the skeleton) and we navigate there. The decision reads the identity we already
  // have — it no longer depends on re-fetching `/auth/me` on every navigation, because that
  // re-fetch is now a background re-check and a gate cannot wait for it.
  const forcedTarget = `/${locale}/app/settings/security/password`;
  const blocked = identity?.mustChangePassword === true && pathname !== forcedTarget;
  useEffect(() => { if (blocked) router.replace(forcedTarget); }, [blocked, forcedTarget, router]);

  useEffect(() => {
    // Some protected request came back 401: the platform layer already revoked the credential.
    const invalidate = () => ejectToLogin();
    window.addEventListener(BLANK_AUTH_INVALIDATED_EVENT, invalidate);
    return () => window.removeEventListener(BLANK_AUTH_INVALIDATED_EVENT, invalidate);
  }, [ejectToLogin]);

  useEffect(() => {
    // Another tab touched the credential: a different non-empty value means "someone else logged
    // in over there", an empty one means "logged out over there". Both are handled the same way —
    // this page is still painting the old user's shell while the credential is no longer his.
    // Drop the snapshot and reload so `/auth/me` answers who this is now. Left alone, this page
    // would keep the previous user's sidebar until the next request happened to hit a 401.
    const onStorage = (event: StorageEvent) => {
      if (event.key !== AUTH_TOKEN_STORAGE_KEY) return;
      if (event.newValue === event.oldValue) return;
      clearCachedIdentity();
      window.location.reload();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return { identity: blocked ? null : identity, permissionUrlPending, refreshIdentity };
}

/**
 * Can the zero-grant onboarding page be painted? Its entire content is the request URL, so this
 * is the only screen that waits for `/auth/session`; anyone with business access already got the
 * shell several lines earlier.
 */
export function onboardingReady(identity: ShellIdentity, permissionUrlPending: boolean): boolean {
  return !permissionUrlPending || identity.permissionRequestUrl !== null;
}
