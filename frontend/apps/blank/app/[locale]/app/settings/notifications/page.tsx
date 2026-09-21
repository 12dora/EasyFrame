"use client";

import { EnterpriseNotificationSettingsSurface } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { localeOf, messages } from "../../../../../lib/messages";
import { notificationSettingsAdapter } from "../../../../../lib/shell-adapter";

/**
 * 设置 → 通知。
 *
 * 与「外观」一样没有权限门禁:后端的 `GET /api/v1/notification-settings` 已经按
 * `gate_permission` 过滤了分组,前端再判一次只会把"看得见分组却进不去页"和"进得去页却
 * 一个分组都没有"两种情况混成同一个 403。一个分组都没有的账号看到的是空状态。
 *
 * 「平台配置」页签由响应里的 `canManage` 决定,同样由后端说了算;`locale` 只用来在后端
 * 下发的双语文本里挑一边。页面的唯一 H1 来自共享面自带的 `PageHeader`,外层宽度容器是
 * 设置段 `layout.tsx` 的 `BlankSettingsFrame`。
 */
export default function NotificationSettingsPage() {
  const params = useParams<{ locale: string }>();
  const locale = localeOf(params.locale);
  const t = messages(locale);
  return (
    <EnterpriseNotificationSettingsSurface
      adapter={notificationSettingsAdapter}
      labels={t.notificationSettings}
      locale={locale}
    />
  );
}
