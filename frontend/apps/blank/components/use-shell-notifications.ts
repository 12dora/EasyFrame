"use client";

import type { EnterpriseNotification, EnterpriseTopbarActionsProps } from "@easy-enterprise/ui/enterprise";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { scheduleWhenIdle } from "../lib/identity-cache";
import { dismissAllNotifications, dismissNotification, loadNotifications } from "../lib/shell-adapter";

/** 一份通知状态连同它所属的范围(账号 + 权限开关);范围对不上的状态一律当作空。 */
interface NotificationState { scope: string; items: EnterpriseNotification[]; loading: boolean; error: boolean }
interface PendingLoad { scope: string; settled: Promise<void> }

/**
 * 通知中心数据与乐观忽略。
 *
 * 只在顶栏组件里调用:通知的加载态翻转只重画顶栏,不牵动外壳与页面(`children`)。
 * 首次拉取排到浏览器空闲之后 —— 角标不影响首屏,不和页面的第一批数据请求抢。
 *
 * 换人(`accountKey`,身份的 `accountId`)或开关权限时清空重拉,且上一个范围的数据绝不落进新范围:
 * - 状态自带 `scope`,输出时按当前范围派生 —— 旧响应无论在哪个时刻落地(渲染与 effect 之间的微任务也算),
 *   只会写进旧范围那份,新范围看到的仍是空;
 * - 新范围的首拉先等上一个范围的在途请求结束再发:`platformRequest` 按「路径 + token」合并在途 GET,
 *   对账换人而 token 未轮换时,不等就会直接领到上一个人的那次响应。
 */
export function useShellNotifications(enabled: boolean, viewAllHref: string, accountKey: string): EnterpriseTopbarActionsProps["notifications"] {
  // 范围每变一次就换一代(渲染期同步,React「随 props 重置 state」模式):权限关了又开回到同一个账号,
  // 也是新的一代,上一代在途的响应同样落不进来。
  const range = `${enabled ? 1 : 0}:${accountKey}`;
  const [seen, setSeen] = useState({ range, epoch: 0 });
  if (seen.range !== range) setSeen({ range, epoch: seen.epoch + 1 });
  const scope = `${seen.epoch}:${range}`;
  const [state, setState] = useState<NotificationState>({ scope, items: [], loading: false, error: false });
  const pending = useRef<PendingLoad | null>(null);
  const current = state.scope === scope ? state : null;
  const update = useCallback((patch: (state: NotificationState) => Partial<NotificationState>) => {
    setState((previous) => previous.scope === scope ? { ...previous, ...patch(previous) } : previous);
  }, [scope]);
  const refresh = useCallback(async () => {
    const previous = pending.current;
    const request = (async () => {
      if (previous && previous.scope !== scope) await previous.settled;
      return loadNotifications();
    })();
    pending.current = { scope, settled: request.then(() => undefined, () => undefined) };
    setState((prior) => ({ scope, items: prior.scope === scope ? prior.items : [], loading: true, error: false }));
    try {
      const items = await request;
      update(() => ({ items }));
    } catch {
      update(() => ({ error: true }));
    } finally {
      update(() => ({ loading: false }));
    }
  }, [scope, update]);
  useEffect(() => {
    if (!enabled) return;
    return scheduleWhenIdle(() => void refresh());
  }, [enabled, refresh]);
  const items = current?.items ?? EMPTY;
  const loading = current?.loading ?? false;
  const error = current?.error ?? false;
  return useMemo(() => enabled ? {
    items,
    loading,
    error,
    viewAllHref,
    onOpen: () => void refresh(),
    onDismiss: async (id: string) => {
      update((prior) => ({ items: prior.items.filter((item) => item.id !== id) }));
      await dismissNotification(id).catch(() => void refresh());
    },
    onDismissAll: async () => {
      update(() => ({ items: [] }));
      await dismissAllNotifications().catch(() => void refresh());
    },
  } : undefined, [enabled, error, items, loading, refresh, update, viewAllHref]);
}

const EMPTY: EnterpriseNotification[] = [];
