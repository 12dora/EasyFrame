"use client";

import {
  EnterpriseGeneralSettingsSurface,
  EnterprisePermissionDeniedPage,
  type EnterpriseGeneralSettingsAdapter,
} from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { loadGeneralSettings, saveGeneralSettings } from "../../../../../lib/shell-adapter";

// 保存成功后由 surface 调用 primeEnterpriseGeneralSettings，顶栏与页脚立即跟随，
// 宿主不需要再自行广播更新事件。
const adapter: EnterpriseGeneralSettingsAdapter = { load: loadGeneralSettings, save: saveGeneralSettings };

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
  return <EnterpriseGeneralSettingsSurface adapter={adapter} labels={t.generalSettings} />;
}
