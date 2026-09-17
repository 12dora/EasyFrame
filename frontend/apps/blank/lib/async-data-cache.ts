/**
 * `useAsyncData` 的内存缓存(stale-while-revalidate 的「stale」那一半)。
 *
 * key 是去掉 `/api/v1` 前缀的请求路径加查询串(如 `/notifications?limit=100`、`/orders/${id}`):
 * 同一个 key 就是同一份数据,回到页面时先画上一次的结果再后台复查。
 * 只存在模块内存里(随页面加载结束),不落盘。契约见 `docs/SHELL_READ_CACHE.md`(C1)。
 * 与钩子分文件,是为了让身份存储(`identity-cache.ts`)能在换人 / 登出时清掉它,而不必依赖 React。
 */

/** 条目上限:超出按最久未写入淘汰,长时间停留的标签页不会无限长大。 */
const MAX_ENTRIES = 100;

const entries = new Map<string, unknown>();
/** 每次清空 / 失效递增;在途请求据此判断自己的结果还能不能写回缓存。 */
let generation = 0;

/** 挂着的 `useAsyncData` 要知道的缓存事件:整份清空(换人 / 登出),或按前缀失效(写操作之后)。 */
export type AsyncDataEvent = { readonly kind: "clear"; readonly refetch: boolean } | { readonly kind: "invalidate"; readonly prefix: string };

const listeners = new Set<(event: AsyncDataEvent) => void>();

/** 订阅缓存事件;返回退订函数。 */
export function subscribeAsyncData(listener: (event: AsyncDataEvent) => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function emit(event: AsyncDataEvent): void {
  for (const listener of [...listeners]) listener(event);
}

/** 存一份深拷贝:调用方就地改自己手里的数据(排序、表单回填)不会污染缓存。 */
function snapshot(value: unknown): unknown {
  if (value === undefined || value === null || typeof structuredClone !== "function") return value;
  try {
    return structuredClone(value);
  } catch {
    return value;
  }
}

export function asyncDataGeneration(): number {
  return generation;
}

export function hasAsyncData(key: string): boolean {
  return entries.has(key);
}

export function readAsyncData<T>(key: string): T | undefined {
  return entries.get(key) as T | undefined;
}

/**
 * 写入一份结果。`startedAt` 是请求发出时的代号:期间发生过清空或失效(登出、换人、写操作后
 * 主动失效),这份结果可能属于上一个人或写之前,不再进缓存。
 */
export function writeAsyncData(key: string, value: unknown, startedAt: number): void {
  if (startedAt !== generation) return;
  entries.delete(key);
  entries.set(key, snapshot(value));
  if (entries.size <= MAX_ENTRIES) return;
  const oldest = entries.keys().next().value;
  if (oldest !== undefined) entries.delete(oldest);
}

export function dropAsyncData(key: string): void {
  entries.delete(key);
}

/**
 * 登出 / 换人时整份清掉:下一个人绝不能先看到上一个人的列表。
 *
 * 挂着的 `useAsyncData` 同步丢掉手里的数据、作废在途响应;`refetch` 为真(默认,换人)时立即
 * 按新身份重取。登出 / 被踢时传 `{ refetch: false }`:页面马上就要离开,此刻重取只会带着空凭据
 * 撞 401,再触发一次静默复查。
 */
export function clearAsyncDataCache({ refetch = true }: { refetch?: boolean } = {}): void {
  generation += 1;
  entries.clear();
  emit({ kind: "clear", refetch });
}

/**
 * 按前缀失效(例如写操作之后 `invalidateAsyncData("/orders")`)。
 * 键命中前缀的 `useAsyncData` 立即复查,写之前发出的在途响应不再落地。
 */
export function invalidateAsyncData(prefix: string): void {
  generation += 1;
  for (const key of [...entries.keys()]) if (key.startsWith(prefix)) entries.delete(key);
  emit({ kind: "invalidate", prefix });
}

/**
 * 写接口回传了整份新数据时直接写回缓存(write-through)。
 *
 * 不递增代号、不发事件:别的键的在途请求照常落地。同一个键上写之前发出的读请求由调用方
 * 作废(`reload()` 让旧响应失效),否则它落地时仍会把缓存盖回写之前的样子。
 */
export function replaceAsyncData(key: string, value: unknown): void {
  writeAsyncData(key, value, generation);
}
