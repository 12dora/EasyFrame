"use client";

import type { EnterpriseNotification, EnterpriseTopbarActionsProps } from "@easy-enterprise/ui/enterprise";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { scheduleWhenIdle } from "../lib/identity-cache";
import { dismissAllNotifications, dismissNotification, loadNotifications } from "../lib/shell-adapter";

/**
 * 通知中心数据与乐观忽略。
 *
 * 只在顶栏组件里调用:通知的加载态翻转只重画顶栏,不牵动外壳与页面(`children`)。
 * 首次拉取排到浏览器空闲之后 —— 角标不影响首屏,不和页面的第一批数据请求抢。
 * `accountKey`(身份的 `accountId`)或 `enabled` 一变就清空重拉:同标签页对账换了人时,
 * 铃铛里不能留着上一个人的通知;上一轮还在途的响应也不再落地。
 */
export function useShellNotifications(enabled: boolean, viewAllHref: string, accountKey: string): EnterpriseTopbarActionsProps["notifications"] {
  const [items, setItems] = useState<EnterpriseNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  // 换人 / 开关权限时在渲染期同步清空(React 官方「随 props 重置 state」模式),不闪旧数据。
  const scope = `${enabled ? 1 : 0}:${accountKey}`;
  const [seenScope, setSeenScope] = useState(scope);
  if (seenScope !== scope) {
    setSeenScope(scope);
    setItems([]);
    setError(false);
    setLoading(false);
  }
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = generation.current;
    setLoading(true);
    setError(false);
    try {
      const next = await loadNotifications();
      if (current === generation.current) setItems(next);
    } catch {
      if (current === generation.current) setError(true);
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    generation.current += 1;
    if (!enabled) return;
    return scheduleWhenIdle(() => void refresh());
  }, [accountKey, enabled, refresh]);
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
