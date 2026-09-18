"use client";

import { EnterpriseAppearanceSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { localeOf, messages } from "../../../../../lib/messages";

/**
 * 设置 → 外观。
 *
 * 只改当前账号自己的观感偏好(表格行高),所以没有权限门禁 —— 任何登录用户都能进。
 * 档位读写的是外壳挂着的 `TableDensityProvider`(值来自 `/auth/session` 的
 * `preferences`,写回走 `PATCH /auth/preferences`),所以这里改完,所有列表页当场跟着变。
 */
export default function AppearanceSettingsPage() {
  const params = useParams<{ locale: string }>();
  const t = messages(localeOf(params.locale));
  return <EnterpriseAppearanceSettingsSurface labels={t.appearanceSettings} />;
}
