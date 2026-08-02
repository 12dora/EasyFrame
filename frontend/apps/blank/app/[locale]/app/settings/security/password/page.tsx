"use client";
import { completeEnterprisePasswordChange, EnterpriseChangePasswordForm, EnterprisePasswordRecoverySurface } from "@easy-enterprise/ui/enterprise";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { localeOf, messages } from "../../../../../../lib/messages";
import { changePassword } from "../../../../../../lib/security-adapter";
import { useBlankShellIdentity } from "../../../../../../components/blank-shell";
import { clearAuthMethod, logout } from "../../../../../../lib/auth-adapter";
export default function ChangePasswordPage() { const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale); const router = useRouter(); const identity = useBlankShellIdentity(); const securityHref = `/${locale}/app/settings/security`; const forced = identity.mustChangePassword; return <div data-test-id="blank-change-password-page"><EnterprisePasswordRecoverySurface title={t.security.password.submit} forced={forced} forcedNotice={t.forcedPasswordNotice} backAction={<Link href={securityHref} className="inline-flex h-7 items-center rounded-[2px] px-2.5 text-[12px] font-medium text-ink-soft transition-colors hover:bg-ink/[0.04] hover:text-ink" data-test-id="change-password-back">{t.navigation.backToSecurity}</Link>}><EnterpriseChangePasswordForm labels={t.security.password} onSubmit={changePassword} onSuccess={() => completeEnterprisePasswordChange({ clearLocalSession: logout, clearAuthMethod, navigate: (href) => router.replace(href), loginTarget: `/${locale}/login?password_changed=1` })}/></EnterprisePasswordRecoverySurface></div>; }
