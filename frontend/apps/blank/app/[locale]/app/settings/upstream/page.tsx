"use client";

import { EnterpriseUpstreamHealthController } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { upstreamHealthAdapter } from "../../../../../lib/upstream-adapter";

export default function UpstreamPage() {
  const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale); const identity = useBlankShellIdentity();
  const permissions = identity.permissions;
  return <EnterpriseUpstreamHealthController adapter={upstreamHealthAdapter} locale={locale} canView={permissions.has("ops.upstream_health.view")} canManage={permissions.has("ops.upstream_health.manage")} labels={t.upstream} actionTestId="upstream-health-refresh" />;
}
