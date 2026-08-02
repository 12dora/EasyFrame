"use client";

import { EnterpriseConfiguredFooter, EnterprisePublicShell, EnterpriseTopbarActions } from "@easy-enterprise/ui/enterprise";
import { Topbar } from "@easy-enterprise/ui/shell";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { loadFooterSettings, type ShellFooterSettings } from "../lib/shell-adapter";
import { localeOf, messages } from "../lib/messages";

export function BlankPublicShell({ children, locale: rawLocale }: { children: ReactNode; locale: string }) {
  const locale = localeOf(rawLocale);
  const labels = messages(locale);
  const pathname = usePathname();
  const router = useRouter();
  const [footer, setFooter] = useState<ShellFooterSettings | null>(null);

  useEffect(() => {
    let active = true;
    loadFooterSettings().then((value) => { if (active) setFooter(value); }).catch(() => { if (active) setFooter(null); });
    return () => { active = false; };
  }, []);

  const footerHtml = locale === "en" ? footer?.footerHtmlEn : footer?.footerHtmlZh;
  const topbar = (
    <Topbar
      testId="public-top-nav"
      brand={<Link href={`/${locale}/login`} className="min-w-0"><div className="truncate text-[16px] font-semibold text-ink">{labels.brand}</div><p className="hidden text-[12px] text-ink-faint sm:block">{labels.subtitle}</p></Link>}
      actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={labels.shell} renderLink={({ href, className, testId, role, children: label }) => <Link href={href} className={className} data-test-id={testId} role={role}>{label}</Link>} />}
    />
  );
  return <EnterprisePublicShell contentAs="div" topbar={topbar} footer={<EnterpriseConfiguredFooter html={footerHtml} fallback={<>{labels.public.footer} · © {new Date().getFullYear()}</>}/>} >{children}</EnterprisePublicShell>;
}

function localizedLocation(pathname: string, locale: string, nextLocale: string) {
  const suffix = typeof window === "undefined" ? "" : window.location.search + window.location.hash;
  return `${pathname.replace(`/${locale}`, `/${nextLocale}`)}${suffix}`;
}
