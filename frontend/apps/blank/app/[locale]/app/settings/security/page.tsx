"use client";

import { completeEnterprisePasswordChange, EnterpriseAccountSecuritySurface } from "@easy-enterprise/ui/enterprise";
import { useParams, useRouter } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { enterpriseSecurityAdapter } from "../../../../../lib/security-adapter";
import { clearAuthMethod, logout } from "../../../../../lib/auth-adapter";

export default function SecurityPage() {
  const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale);
  const identity = useBlankShellIdentity();
  const router = useRouter();
  const permissions = identity.permissions;
  const capability = identity.securityCapabilities;
  return <EnterpriseAccountSecuritySurface adapter={enterpriseSecurityAdapter} onPasswordChanged={() => completeEnterprisePasswordChange({ clearLocalSession: logout, clearAuthMethod, navigate: (href) => router.replace(href), loginTarget: `/${locale}/login?password_changed=1` })} permissions={{ password: identity.hasLocalPassword && capability.passwordChange, totpStatus: permissions.has("auth.totp.advance") && capability.totpStatus, createTotp: permissions.has("auth.totp.create") && capability.totpEnroll, disableTotp: permissions.has("auth.totp.advance") && capability.totpDisable, viewPasskeys: permissions.has("auth.passkey.view") && capability.passkeyList, canRegisterPasskeys: permissions.has("auth.passkey.create") && capability.passkeyRegister, canDeletePasskeys: permissions.has("auth.passkey.create") && capability.passkeyDelete }} labels={t.security} />;
}
