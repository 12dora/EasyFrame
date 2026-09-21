"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { identitySessionGeneration, patchCachedPreference } from "../lib/identity-cache";
import type { ShellIdentity } from "../lib/shell-adapter";

type PreferenceKey = "tableDensity" | "rowSpacing";
type PreferenceValue = ShellIdentity[PreferenceKey];
type Pending = { identity: ShellIdentity; locale: string; generation: number; value: PreferenceValue; saving: boolean };

/** One write per preference; completed optimism lasts only until the next identity refresh. */
export function useAccountPreference(
  identity: ShellIdentity, locale: string, key: PreferenceKey,
  save: (value: PreferenceValue) => Promise<Record<PreferenceKey, PreferenceValue>>,
  onError: () => void,
) {
  const sessionGeneration = identitySessionGeneration();
  const [pending, setPending] = useState<Pending | null>(null);
  const currentIdentity = useRef(identity);
  const lifecycle = useRef({ active: true, saving: false });
  useEffect(() => { currentIdentity.current = identity; }, [identity]);
  useEffect(() => {
    const current = { active: true, saving: false };
    lifecycle.current = current;
    return () => { current.active = false; };
  }, [identity.accountId, locale, sessionGeneration]);

  const change = useCallback(async (next: PreferenceValue) => {
    const current = lifecycle.current;
    if (!current.active || current.saving) return;
    current.saving = true;
    const generation = identitySessionGeneration();
    const valid = () => current.active && generation === identitySessionGeneration();
    setPending({ identity, locale, generation, value: next, saving: true });
    try {
      const session = await save(next);
      if (!valid()) return;
      setPending({ identity: currentIdentity.current, locale, generation, value: session[key], saving: false });
      patchCachedPreference(locale, identity.accountId, generation, key, session[key]);
    } catch {
      if (!valid()) return;
      setPending(null);
      onError();
    } finally {
      current.saving = false;
    }
  }, [identity, locale, key, save, onError]);

  const applicable = pending !== null && pending.generation === sessionGeneration
    && pending.locale === locale && pending.identity.accountId === identity.accountId
    && (pending.saving || pending.identity === identity);
  return {
    value: applicable ? pending.value : identity[key],
    saving: Boolean(applicable && pending.saving),
    change,
  };
}
