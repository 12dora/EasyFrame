"use client";

import { InlineNotice } from "@easy-enterprise/ui";
import { EnterpriseNotificationCenter } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { useBlankShellIdentity } from "../../../../components/blank-shell";
import { localeOf, messages } from "../../../../lib/messages";
import { dismissNotification, loadNotifications } from "../../../../lib/shell-adapter";
import { replaceAsyncData, useAsyncData } from "../../../../lib/use-async-data";

/** 与 `loadNotifications` 的真实请求同源:去掉 `/api/v1` 的路径 + 查询串。 */
const NOTIFICATIONS_KEY = "/notifications?limit=100";

export default function NotificationsPage() {
  const params = useParams<{ locale: string }>(); const t = messages(localeOf(params.locale)); const identity = useBlankShellIdentity();
  const canView = identity.permissions.has("notification.center.view");
  const notifications = useAsyncData(loadNotifications, canView, { cacheKey: NOTIFICATIONS_KEY });
  // 乐观移除:点掉的先藏起来,成功后把剩下的写回缓存(回到本页不再闪出来),失败再重取。
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());
  const confirmed = useRef(new Set<string>());
  const items = (notifications.data ?? []).filter((item) => !dismissed.has(item.id));
  const { data, reload } = notifications;
  const onDismiss = useCallback(async (id: string) => {
    setDismissed((current) => new Set(current).add(id));
    try {
      await dismissNotification(id);
      confirmed.current.add(id);
      if (data) replaceAsyncData(NOTIFICATIONS_KEY, data.filter((item) => !confirmed.current.has(item.id)));
    } catch {
      setDismissed((current) => { const next = new Set(current); next.delete(id); return next; });
      reload();
    }
  }, [data, reload]);
  if (!canView) return <InlineNotice tone="error" message={t.common.permissionDenied} data-test-id="permission-denied" />;
  return <EnterpriseNotificationCenter {...t.notifications} items={items} loading={notifications.loading} error={notifications.error} onRetry={reload} onDismiss={onDismiss} />;
}
