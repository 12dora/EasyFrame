"use client";

import { EnterpriseOidcCompleteController } from "@easy-enterprise/ui/enterprise";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { persistAuthToken, rememberAuthMethod } from "../lib/auth-adapter";
import { localeOf, messages, type Locale } from "../lib/messages";
import { BlankPublicShell } from "./blank-public-shell";

const rememberOidc = () => rememberAuthMethod("oidc");

export function OidcComplete({ locale: requestedLocale }: { locale?: string }) {
  const router = useRouter(); const locale: Locale = requestedLocale ? localeOf(requestedLocale) : browserLocale(); const labels = messages(locale).oidcComplete;
  return <BlankPublicShell locale={locale}><EnterpriseOidcCompleteController labels={labels} defaultTarget={`/${locale}/app`} persistToken={persistAuthToken} rememberOidc={rememberOidc} navigate={(href) => router.replace(href)} renderBackLink={(label) => <Link href={`/${locale}/login`} className="inline-flex h-9 items-center justify-center rounded-[2px] border border-ink bg-ink px-4 text-[13px] font-medium text-paper" data-test-id="oidc-complete-back-to-login">{label}</Link>} /></BlankPublicShell>;
}

function browserLocale(): Locale {
  if (typeof document !== "undefined") { const saved = document.cookie.match(/(?:^|; )NEXT_LOCALE=([^;]+)/)?.[1]; if (saved) return localeOf(decodeURIComponent(saved)); }
  return typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("en") ? "en" : "zh-CN";
}
