"use client";

import { EnterpriseGeneralSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { InlineNotice } from "@easy-enterprise/ui";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { platformRequest } from "../../../../../lib/platform-api";
import { type ShellGeneralSettings } from "../../../../../lib/shell-adapter";

const adapter = {
  load: () => platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general"),
  save: (value: ShellGeneralSettings) =>
    platformRequest<ShellGeneralSettings>("/api/v1/app-settings/general", { method: "PUT", body: JSON.stringify(value) }),
  onSaved: (value: ShellGeneralSettings) =>
    window.dispatchEvent(new CustomEvent("enterprise-starter:general-updated", { detail: value })),
};

export default function GeneralSettingsPage() {
  const params = useParams<{ locale: string }>();
  const t = messages(localeOf(params.locale));
  const labels = t.generalSettings;
  const identity = useBlankShellIdentity();
  if (!identity.permissions.has("settings.app_setting.update")) return <InlineNotice tone="error" message={t.common.permissionDenied} data-test-id="permission-denied" />;
  return <EnterpriseGeneralSettingsSurface adapter={adapter} labels={labels} />;
}
