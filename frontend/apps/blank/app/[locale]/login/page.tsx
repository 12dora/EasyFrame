"use client";

import { EnterpriseLoginController, safeInternalTarget } from "@easy-enterprise/ui/enterprise";
import { PageLoadingSkeleton } from "@easy-enterprise/ui";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { enterpriseLoginAdapter } from "../../../lib/auth-adapter";
import { localeOf, messages } from "../../../lib/messages";
import { BlankPublicShell } from "../../../components/blank-public-shell";

/**
 * 登录页读 `?next=` 与 OIDC 回跳参数(`useSearchParams`),Next 要求外层有 Suspense 边界。
 * 这是公开页,不在外壳段的 `force-dynamic` 之下;骨架屏只在首次加载时出现。
 */
export default function LoginPage() {
  return <Suspense fallback={<PageLoadingSkeleton/>}><LoginController/></Suspense>;
}

function LoginController() {
  const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale); const router = useRouter(); const search = useSearchParams();
  const target = safeInternalTarget(search.get("next"), `/${locale}/app`);
  return <BlankPublicShell locale={locale}><main><EnterpriseLoginController adapter={enterpriseLoginAdapter} target={target} changePasswordTarget={`/${locale}/app/settings/security/password`} navigate={(href) => router.push(href)} oidcError={{ kind: search.get("oidc_error"), detail: search.get("oidc_error_detail") }} successNotice={search.get("password_changed") === "1" ? t.security.password.success : null} labels={t.login} /></main></BlankPublicShell>;
}
