"use client";

import { EnterpriseAccessSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { authorizationAdapter } from "../../../../../lib/authorization-adapter";
import { localeOf, messages } from "../../../../../lib/messages";

export default function AccessPage() {
  const params = useParams<{ locale: string }>();
  const locale = localeOf(params.locale);
  const t = messages(locale);
  const permissions = useBlankShellIdentity().permissions;
  return (
    <EnterpriseAccessSettingsSurface
      adapter={authorizationAdapter}
      labels={{
        ...t.access,
      }}
      permissions={{
        viewIdentity: permissions.has("identity.integration.view"),
        manageIdentity: permissions.has("identity.integration.manage"),
        viewAuthorization: permissions.has("authz.integration.view"),
        manageAuthorization: permissions.has("authz.integration.manage"),
      }}
      locale={locale}
    />
  );
}
