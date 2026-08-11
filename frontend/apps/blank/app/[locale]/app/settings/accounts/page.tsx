"use client";

import { EnterpriseLocalAccountsSurface } from "@easy-enterprise/ui/enterprise-local-accounts";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import {
  LOCAL_ACCOUNT_BASELINE_PERMISSIONS,
  localAccountsAdapter,
} from "../../../../../lib/local-accounts-adapter";
import { localeOf, messages } from "../../../../../lib/messages";

export default function LocalAccountsPage() {
  const params = useParams<{ locale: string }>();
  const locale = localeOf(params.locale);
  const t = messages(locale);
  const identity = useBlankShellIdentity();
  const permissions = identity.permissions;
  return (
    <EnterpriseLocalAccountsSurface
      adapter={localAccountsAdapter}
      labels={t.localAccounts}
      permissions={{
        view: permissions.has("accounts.local.view"),
        manage: permissions.has("accounts.local.manage"),
      }}
      capabilities={{
        isLocalSuperadmin: identity.isLocalSuperadmin,
        accountId: identity.accountId,
      }}
      locale={locale}
      baselinePermissions={LOCAL_ACCOUNT_BASELINE_PERMISSIONS}
    />
  );
}
