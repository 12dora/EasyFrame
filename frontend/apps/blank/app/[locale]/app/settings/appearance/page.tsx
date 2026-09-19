"use client";

import { EnterpriseAppearanceSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useBlankShellIdentity } from "../../../../../components/blank-shell";
import { localeOf, messages } from "../../../../../lib/messages";
import { generalSettingsAdapter } from "../../../../../lib/shell-adapter";

/**
 * 设置 → 外观。
 *
 * 表格行高是当前账号自己的观感偏好,所以页面本身没有权限门禁 —— 任何登录用户都能进。
 * 档位读写的是外壳挂着的 `TableDensityProvider`(值来自 `/auth/session` 的
 * `preferences`,写回走 `PATCH /auth/preferences`),所以这里改完,所有列表页当场跟着变。
 *
 * 「显示页脚」是**全局**设置(存在通用设置里,与「设置 → 通用」同一个 adapter、同一个 PUT),
 * 只给有 `settings.app_setting.update` 的人看;没有这条权限时整张卡片不画、也不发请求。
 * 切换即保存,外壳当场收起 / 放出页脚。
 */
export default function AppearanceSettingsPage() {
  const params = useParams<{ locale: string }>();
  const t = messages(localeOf(params.locale));
  const identity = useBlankShellIdentity();
  return (
    <EnterpriseAppearanceSettingsSurface
      labels={t.appearanceSettings}
      canManageGlobal={identity.permissions.has("settings.app_setting.update")}
      generalSettingsAdapter={generalSettingsAdapter}
    />
  );
}
