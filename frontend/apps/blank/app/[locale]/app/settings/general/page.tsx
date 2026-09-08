"use client";

import { EnterpriseGeneralSettingsSurface, type EnterpriseGeneralSettingsAdapter } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { InlineNotice } from "@easy-enterprise/ui";
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
  if (!identity.permissions.has("settings.app_setting.update")) return <InlineNotice tone="error" message={t.common.permissionDenied} data-test-id="permission-denied" />;
  return <EnterpriseGeneralSettingsSurface adapter={adapter} labels={t.generalSettings} />;
}
