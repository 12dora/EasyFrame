"use client";

import { InlineNotice } from "@easy-enterprise/ui";
import { EnterpriseNotificationCenter, type EnterpriseNotification } from "@easy-enterprise/ui/enterprise";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useBlankShellIdentity } from "../../../../components/blank-shell";
import { localeOf, messages } from "../../../../lib/messages";
import { dismissNotification, loadNotifications } from "../../../../lib/shell-adapter";

export default function NotificationsPage() {
  const params = useParams<{ locale: string }>(); const t = messages(localeOf(params.locale)); const identity = useBlankShellIdentity(); const [items, setItems] = useState<EnterpriseNotification[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(false);
  const refresh = useCallback(async () => { setLoading(true); setError(false); try { setItems(await loadNotifications()); } catch { setError(true); } finally { setLoading(false); } }, []);
  useEffect(() => { if (identity.permissions.has("notification.center.view")) void refresh(); }, [identity, refresh]);
  if (!identity.permissions.has("notification.center.view")) return <InlineNotice tone="error" message={t.common.permissionDenied} data-test-id="permission-denied" />;
  return <EnterpriseNotificationCenter {...t.notifications} items={items} loading={loading} error={error} onRetry={refresh} onDismiss={async (id) => { setItems((current) => current.filter((item) => item.id !== id)); await dismissNotification(id).catch(() => void refresh()); }} />;
}
