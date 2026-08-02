"use client";

import { EnterpriseLoginController, safeInternalTarget } from "@easy-enterprise/ui/enterprise";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { enterpriseLoginAdapter } from "../../../lib/auth-adapter";
import { localeOf, messages } from "../../../lib/messages";
import { BlankPublicShell } from "../../../components/blank-public-shell";

export default function LoginPage() {
  const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale); const router = useRouter(); const search = useSearchParams();
  const target = safeInternalTarget(search.get("next"), `/${locale}/app`);
  return <BlankPublicShell locale={locale}><main><EnterpriseLoginController adapter={enterpriseLoginAdapter} target={target} changePasswordTarget={`/${locale}/app/settings/security/password`} navigate={(href) => router.push(href)} oidcError={{ kind: search.get("oidc_error"), detail: search.get("oidc_error_detail") }} successNotice={search.get("password_changed") === "1" ? t.security.password.success : null} labels={t.login} /></main></BlankPublicShell>;
}
