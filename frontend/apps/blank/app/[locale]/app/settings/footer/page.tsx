"use client";

import { EnterpriseFooterSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { InlineNotice } from "@easy-enterprise/ui";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { platformRequest } from "../../../../../lib/platform-api";

const adapter = {
  load: () => platformRequest<{ footerHtmlZh: string; footerHtmlEn: string }>("/api/v1/app-settings/footer"),
  save: (value: { footerHtmlZh: string; footerHtmlEn: string }) => platformRequest<{ footerHtmlZh: string; footerHtmlEn: string }>("/api/v1/app-settings/footer", { method: "PUT", body: JSON.stringify(value) }),
  onSaved: (value: { footerHtmlZh: string; footerHtmlEn: string }) => window.dispatchEvent(new CustomEvent("enterprise-starter:footer-updated", { detail: value })),
};

export default function FooterSettingsPage() {
  const params = useParams<{ locale: string }>();
  const t = messages(localeOf(params.locale)); const labels = t.footerSettings; const identity = useBlankShellIdentity();
  if (!identity.permissions.has("settings.app_setting.update")) return <InlineNotice tone="error" message={t.common.permissionDenied} data-test-id="permission-denied" />;
  return <EnterpriseFooterSettingsSurface adapter={adapter} labels={labels} />;
}
