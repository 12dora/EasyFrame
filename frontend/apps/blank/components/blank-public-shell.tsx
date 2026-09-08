"use client";

import { EnterpriseConfiguredFooter, EnterprisePublicShell, EnterpriseTopbarActions } from "@easy-enterprise/ui/enterprise";
import { Topbar } from "@easy-enterprise/ui/shell";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { loadGeneralSettings, type ShellGeneralSettings } from "../lib/shell-adapter";
import { localeOf, messages } from "../lib/messages";

export function BlankPublicShell({ children, locale: rawLocale }: { children: ReactNode; locale: string }) {
  const locale = localeOf(rawLocale);
  const labels = messages(locale);
  const pathname = usePathname();
  const router = useRouter();
  const [settings, setSettings] = useState<ShellGeneralSettings | null>(null);

  useEffect(() => {
    let active = true;
    loadGeneralSettings().then((value) => { if (active) setSettings(value); }).catch(() => { if (active) setSettings(null); });
    return () => { active = false; };
  }, []);

  const footerHtml = locale === "en" ? settings?.footerHtmlEn : settings?.footerHtmlZh;
  const topbar = (
    <Topbar
      testId="public-top-nav"
      brand={
        // EasyUI EnterpriseBrandSlot wiring: see docs/GENERAL_SETTINGS.md
        <Link href={`/${locale}/login`} className="min-w-0"><div className="truncate text-[16px] font-semibold text-ink">{labels.brand}</div><p className="hidden text-[12px] text-ink-faint sm:block">{labels.subtitle}</p></Link>
      }
      actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={labels.shell} renderLink={({ href, className, testId, role, children: label }) => <Link href={href} className={className} data-test-id={testId} role={role}>{label}</Link>} />}
    />
  );
  return <EnterprisePublicShell contentAs="div" topbar={topbar} footer={<EnterpriseConfiguredFooter html={footerHtml} fallback={<>{labels.public.footer} · © {new Date().getFullYear()}</>}/>} >{children}</EnterprisePublicShell>;
}

function localizedLocation(pathname: string, locale: string, nextLocale: string) {
  const suffix = typeof window === "undefined" ? "" : window.location.search + window.location.hash;
  return `${pathname.replace(`/${locale}`, `/${nextLocale}`)}${suffix}`;
}
