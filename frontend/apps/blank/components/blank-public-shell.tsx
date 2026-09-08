"use client";

import { EnterpriseBrandSlot, EnterpriseConfiguredFooter, EnterprisePublicShell, EnterpriseTopbarActions, resolveEnterpriseBrand, resolveEnterpriseFooterHtml, useEnterpriseGeneralSettings } from "@easy-enterprise/ui/enterprise";
import { Topbar } from "@easy-enterprise/ui/shell";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import brandLogo from "../assets/brand/jiefa_logo.webp";
import { loadGeneralSettings } from "../lib/shell-adapter";
import { localeOf, messages } from "../lib/messages";

export function BlankPublicShell({ children, locale: rawLocale }: { children: ReactNode; locale: string }) {
  const locale = localeOf(rawLocale);
  const labels = messages(locale);
  const pathname = usePathname();
  const router = useRouter();
  // 与登录后的壳共享同一份通用设置缓存，公开页与应用内品牌永远一致。
  const { settings } = useEnterpriseGeneralSettings(loadGeneralSettings);
  const brand = resolveEnterpriseBrand(settings, locale, { title: labels.brand, subtitle: null, logoSrc: brandLogo.src });

  const topbar = (
    <Topbar
      testId="public-top-nav"
      brand={<EnterpriseBrandSlot href={`/${locale}/login`} title={brand.title} subtitle={brand.subtitle} logoSrc={brand.logoSrc} testId="public-brand" renderLink={({ href, className, children: label, testId }) => <Link href={href} className={className} data-test-id={testId}>{label}</Link>} />}
      actions={<EnterpriseTopbarActions pathKey={pathname} locale={locale} localeOptions={[{ code: "zh-CN", label: "中文" }, { code: "en", label: "English" }]} onLocaleChange={(next) => router.replace(localizedLocation(pathname, locale, String(next)))} labels={labels.shell} renderLink={({ href, className, testId, role, children: label }) => <Link href={href} className={className} data-test-id={testId} role={role}>{label}</Link>} />}
    />
  );
  return <EnterprisePublicShell contentAs="div" topbar={topbar} footer={<EnterpriseConfiguredFooter html={resolveEnterpriseFooterHtml(settings, locale)} fallback={<>{labels.public.footer} · © {new Date().getFullYear()}</>}/>} >{children}</EnterprisePublicShell>;
}

function localizedLocation(pathname: string, locale: string, nextLocale: string) {
  const suffix = typeof window === "undefined" ? "" : window.location.search + window.location.hash;
  return `${pathname.replace(`/${locale}`, `/${nextLocale}`)}${suffix}`;
}
