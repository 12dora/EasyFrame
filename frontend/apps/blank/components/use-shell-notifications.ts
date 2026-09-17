"use client";

import type { EnterpriseNotification, EnterpriseTopbarActionsProps } from "@easy-enterprise/ui/enterprise";
import { useCallback, useEffect, useMemo, useState } from "react";
import { scheduleWhenIdle } from "../lib/identity-cache";
import { dismissAllNotifications, dismissNotification, loadNotifications } from "../lib/shell-adapter";

/**
 * 通知中心数据与乐观忽略。
 *
 * 只在顶栏组件里调用:通知的加载态翻转只重画顶栏,不牵动外壳与页面(`children`)。
 * 首次拉取排到浏览器空闲之后 —— 角标不影响首屏,不和页面的第一批数据请求抢。
 */
export function useShellNotifications(enabled: boolean, viewAllHref: string): EnterpriseTopbarActionsProps["notifications"] {
  const [items, setItems] = useState<EnterpriseNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(false);
    try { setItems(await loadNotifications()); } catch { setError(true); } finally { setLoading(false); }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    return scheduleWhenIdle(() => void refresh());
  }, [enabled, refresh]);
  return useMemo(() => enabled ? {
    items,
    loading,
    error,
    viewAllHref,
    onOpen: () => void refresh(),
    onDismiss: async (id: string) => {
      setItems((current) => current.filter((item) => item.id !== id));
      await dismissNotification(id).catch(() => void refresh());
    },
    onDismissAll: async () => {
      setItems([]);
      await dismissAllNotifications().catch(() => void refresh());
    },
  } : undefined, [enabled, error, items, loading, refresh, viewAllHref]);
}
