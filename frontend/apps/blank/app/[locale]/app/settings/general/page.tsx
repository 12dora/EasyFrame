"use client";

import { EnterpriseGeneralSettingsSurface, EnterprisePermissionDeniedPage } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { generalSettingsAdapter } from "../../../../../lib/shell-adapter";

export default function GeneralSettingsPage() {
  const params = useParams<{ locale: string }>();
  const t = messages(localeOf(params.locale));
  const identity = useBlankShellIdentity();
  // 无权限时仍然是「通用」这一页：标题留在门禁之外，整页始终只有一个 H1。
  if (!identity.permissions.has("settings.app_setting.update")) {
    return (
      <EnterprisePermissionDeniedPage
        title={t.navigation.general}
        description={t.generalSettings.description}
        message={t.common.permissionDenied}
        testId="general-settings-page"
        surface="general-settings"
      />
    );
  }
  return <EnterpriseGeneralSettingsSurface adapter={generalSettingsAdapter} labels={t.generalSettings} />;
}
