"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { asyncDataGeneration, dropAsyncData, hasAsyncData, readAsyncData, subscribeAsyncData, writeAsyncData, type AsyncDataEvent } from "./async-data-cache";

export { clearAsyncDataCache, invalidateAsyncData, replaceAsyncData } from "./async-data-cache";

export interface AsyncData<T> {
  data: T | null;
  loading: boolean;
  /**
   * 画面上是缓存或上一份数据,新结果还在路上(后台复查);此时 `loading` 为 false。
   * `useAsyncData` 一定给出;可选只是为了让手工拼装的 `AsyncData`(包装钩子、测试替身)不必跟着改。
   */
  refreshing?: boolean;
  error: boolean;
  /** 最近一次失败的原因(通常是 PlatformRequestError),供错误态按状态码分流;无错误时为 null。 */
  failure: unknown;
  /** 重新拉取(错误态的「重试」按钮直接绑它)。 */
  reload: () => void;
}

export interface AsyncDataOptions {
  /**
   * 缓存键:去掉 `/api/v1` 前缀的请求路径加查询串(如 `` `/orders?${query}` ``、`` `/orders/${id}` ``)。
   * 给了就启用内存里的 stale-while-revalidate:有缓存先画缓存再后台复查;只改查询串(筛选 / 翻页)时
   * 保留上一份数据而不是闪回加载态。编辑器初始化草稿这类「拿旧数据会写错」的读取不要给。
   */
  cacheKey?: string | null;
}

type Slot<T> = Omit<AsyncData<T>, "reload" | "refreshing"> & { refreshing: boolean };

/** 这几种失败说明「这份数据你已经不该看到」:清掉画面与缓存。其余(网络、5xx)保留上一份数据。 */
const REVOKING_STATUSES: ReadonlySet<number> = new Set([401, 403, 404]);

function loadingSlot<T>(loading: boolean): Slot<T> {
  return { data: null, loading, refreshing: false, error: false, failure: null };
}

/** 缓存命中的状态;`readAsyncData` 给的是深拷贝,画面上那份与缓存互不影响。 */
function cachedSlot<T>(key: string | null, refreshing = true): Slot<T> | null {
  if (!key || !hasAsyncData(key)) return null;
  return { data: readAsyncData<T>(key) ?? null, loading: false, refreshing, error: false, failure: null };
}

/** 键的路径部分(`?` / `#` 之前)。 */
function pathOf(key: string): string {
  return key.split(/[?#]/, 1)[0];
}

/**
 * 换键:新键有缓存就画缓存;没有缓存时,只有路径相同(只改了查询串:筛选 / 翻页)才留着上一份数据
 * 标成复查中 —— 换了资源(`/orders/A` → `/orders/B`)一律回加载态,绝不把 A 的数据画在 B 的地址下。
 */
function rekeyedSlot<T>(previousKey: string | null, key: string | null, previous: Slot<T>): Slot<T> {
  const cached = cachedSlot<T>(key);
  if (cached) return cached;
  const sameResource = previousKey !== null && key !== null && pathOf(previousKey) === pathOf(key);
  if (previous.data === null || !sameResource) return loadingSlot(true);
  return { ...previous, loading: false, refreshing: true, error: false, failure: null };
}

function isRevoking(cause: unknown): boolean {
  const status = typeof cause === "object" && cause !== null ? (cause as { status?: unknown }).status : undefined;
  return typeof status === "number" && REVOKING_STATUSES.has(status);
}

/** 失败:带缓存键且手里有数据、又不是 401/403/404 时保留数据只亮错误;否则清掉数据。 */
function failedSlot<T>(key: string | null, previous: Slot<T>, cause: unknown): Slot<T> {
  if (key && previous.data !== null && !isRevoking(cause)) return { ...previous, loading: false, refreshing: false, error: true, failure: cause };
  return { data: null, loading: false, refreshing: false, error: true, failure: cause };
}

/**
 * 这条缓存事件与本钩子有关吗:清空一律有关;失效只管键命中前缀、且不在失败态的。
 * 已经落在失败态(不论是否还留着数据)的不重取:调用方常在读取失败时顺手失效本资源,
 * 这里再重取就成了「失败 → 失效 → 重取 → 失败」的死循环;要重试走 `reload()`。
 */
function concerns<T>(event: AsyncDataEvent, key: string | null, current: Slot<T>): boolean {
  if (event.kind === "clear") return true;
  if (event.kind === "replace") return key === event.key;
  return key !== null && key.startsWith(event.prefix) && !current.error;
}

function eventSlot<T>(event: AsyncDataEvent, previous: Slot<T>): Slot<T> {
  if (event.kind === "replace") return cachedSlot<T>(event.key, false) ?? previous;
  if (event.kind === "clear" || previous.data === null) return loadingSlot(true);
  return { ...previous, loading: false, refreshing: true, error: false, failure: null };
}

/**
 * 缓存被清空 / 失效 / 写穿时,挂着的钩子也要跟上:作废在途响应(`epoch` 递增),改状态,需要时重取。
 * 只看事件,不看全局代号 —— 别的键的写回(write-through)不该让这里的在途请求悬空。
 */
interface CacheEventWiring<T> {
  enabled: boolean;
  key: string | null;
  state: Slot<T>;
  /** 在途响应的作废计数:事件命中时递增,发出时记下的值对不上就不落地。 */
  epoch: { current: number };
  setState: (update: (previous: Slot<T>) => Slot<T>) => void;
  refetch: () => void;
}

function useCacheEvents<T>({ enabled, key, state, epoch, setState, refetch }: CacheEventWiring<T>): void {
  const latest = useRef(state);
  useEffect(() => { latest.current = state; });
  useEffect(() => {
    if (!enabled) return undefined;
    return subscribeAsyncData((event) => {
      if (!concerns(event, key, latest.current)) return;
      epoch.current += 1;
      setState((previous) => eventSlot(event, previous));
      if (event.kind === "invalidate" || (event.kind === "clear" && event.refetch)) refetch();
    });
  }, [enabled, epoch, key, refetch, setState]);
}

/**
 * 只读数据拉取(契约 C1,见 `docs/SHELL_READ_CACHE.md`)。
 *
 * 用法:`const items = useAsyncData(useCallback(() => loadNotifications(), []), canView, { cacheKey: "/notifications?limit=100" });`
 * `load` 必须是稳定引用(useCallback),否则每次渲染都会重新请求。
 * `enabled` 为 false 时不发请求,`loading` 恒为 false —— 权限门禁直接传进来即可。
 *
 * `enabled` 由 false 翻成 true 是「重新打开」(对话框最典型):上一次的结果已经过期,
 * 所以先回到加载态并丢掉旧数据,调用方才不会拿着上一次的数字做决定(例如删除确认框在影响面重算完之前继续阻断)。
 * `reload()` 不清数据:列表重试时表格不该先闪成空的。
 *
 * 结果只认最后一次发出的请求:键、`load`、令牌一变,或缓存被清空 / 按前缀失效,上一次在途的响应
 * 落地都会被丢弃。`refreshing && data` 时画面上的数据属于缓存或同一路径的上一个查询串。
 * 复查失败:401/403/404 清掉数据与缓存(绝不能继续画已无权看的数据);网络 / 5xx 且带缓存键时
 * 保留数据、只亮 `error`。缓存读写都是深拷贝,调用方就地改自己手里的 `data` 不会污染缓存(但会影响本钩子下一次渲染,仍建议当只读)。
 */
export function useAsyncData<T>(load: () => Promise<T>, enabled = true, options?: AsyncDataOptions): AsyncData<T> & { refreshing: boolean } {
  const key = options?.cacheKey || null;
  const [state, setState] = useState<Slot<T>>(() => (enabled ? cachedSlot<T>(key) : null) ?? loadingSlot(enabled));
  const [token, setToken] = useState(0);
  const epoch = useRef(0);
  const [seen, setSeen] = useState({ enabled, key });
  if (seen.enabled !== enabled || seen.key !== key) {
    // 渲染期同步(不在 effect 里 setState):重新打开 / 换键的那一帧就必须是对的状态。
    setSeen({ enabled, key });
    if (enabled && !seen.enabled) setState(cachedSlot<T>(key) ?? loadingSlot(true));
    else if (enabled) setState((previous) => rekeyedSlot(seen.key, key, previous));
  }
  const refetch = useCallback(() => setToken((value) => value + 1), []);
  useCacheEvents({ enabled, key, state, epoch, setState, refetch });
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const startedEpoch = epoch.current;
    const current = () => alive && epoch.current === startedEpoch;
    const startedAt = asyncDataGeneration();
    load()
      .then((data) => {
        if (!current()) return;
        if (key) writeAsyncData(key, data, startedAt);
        setState({ data, loading: false, refreshing: false, error: false, failure: null });
      })
      .catch((cause: unknown) => {
        if (!current()) return;
        if (key && isRevoking(cause)) dropAsyncData(key);
        setState((previous) => failedSlot(key, previous, cause));
      });
    return () => { alive = false; };
  }, [enabled, key, load, token]);
  const reload = useCallback(() => {
    setState((current) => ({ ...current, loading: true, refreshing: false, error: false, failure: null }));
    refetch();
  }, [refetch]);
  return { ...state, reload };
}
