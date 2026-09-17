# 读取缓存（`useAsyncData` 的 stale-while-revalidate，契约 C1）

同一个标签页里回到看过的页面、翻页或换筛选时，画面不该先闪回加载态再等一次往返。
blank 模板用**模块内存里的读取缓存 + 在途 GET 合并**解决这件事。源头改动来自 EasyLearning
（`perf(shell)` e04df7e + `fix(shell)` 049c1c8 评审整改），已回流到模板。

与 [SHELL_PERCEIVED_LOADING.md](SHELL_PERCEIVED_LOADING.md)（外壳身份快照）是一对：
身份快照让页面早挂上，读取缓存让页面挂上时就有数据。

## 文件

| 文件 | 职责 |
|---|---|
| `lib/async-data-cache.ts` | 缓存本体（不依赖 React）：条目、代号 `generation`、清空 / 失效事件 |
| `lib/use-async-data.ts` | `useAsyncData` 钩子；再导出 `clearAsyncDataCache` / `invalidateAsyncData` / `replaceAsyncData` |
| `lib/platform-api.ts` | `platformRequest` 的在途 GET 合并（`inFlightGets`） |
| `lib/identity-cache.ts` | 换人 / 结束会话时清缓存 |

## 公开契约

```ts
useAsyncData<T>(load: () => Promise<T>, enabled?: boolean, options?: { cacheKey?: string | null }): AsyncData<T> & { refreshing: boolean }
clearAsyncDataCache(opts?: { refetch?: boolean }): void   // 默认 refetch: true
invalidateAsyncData(prefix: string): void
replaceAsyncData(key: string, value: unknown): void
```

`AsyncData<T>` = `{ data, loading, refreshing?, error, failure, reload }`。`load` 必须是稳定引用
（模块级函数或 `useCallback`）；`enabled` 为 false 时不发请求、`loading` 恒为 false（权限门禁直接传进来）。

## 规则

- **键**：`cacheKey` = 去掉 `/api/v1` 前缀的请求路径 + 查询串（`/notifications?limit=100`、`/orders/<id>`），
  与真实请求同源拼出来，换了参数就换了键。不给键就不缓存（行为同改前）。
- **存储**：只在模块内存，不落盘，上限 100 条（按最久未写入淘汰）；写入与命中读取都走 `structuredClone`，
  调用方就地改手里的 `data` 碰不到缓存本体（仍建议当只读）。
- **命中**：挂载时有缓存先画缓存（`loading: false`、`refreshing: true`），同时后台复查。
- **换键**：新键有缓存画缓存；否则**只有路径相同**（只改查询串：筛选 / 翻页）才留着上一份数据标 `refreshing`，
  换了资源（`/orders/A` → `/orders/B`）一律回加载态、`data: null`。
- **失败**：401 / 403 / 404 丢数据并删缓存条目（绝不继续画已无权看的数据）；网络 / 5xx 且手里有数据时
  保留数据、置 `error`。
- **过期响应不落地**：结果只认最后一次发出的请求；请求发出后发生过清空或失效（代号变了），
  响应既不写缓存也不进画面。
- **写操作之后**：当前视图 `reload()`；其他页面可能过期的键用 `invalidateAsyncData(前缀)`
  （挂着的钩子保留数据后台复查；已在失败态的不自动重取，避免「失败 → 失效 → 重取」死循环）。
  写接口回传**整份权威新数据**时用 `replaceAsyncData(key, value)` 写穿：不递增代号（别的键照常落地），
  挂着同一键的钩子改画新数据并作废写之前发出的在途读取。只拿到「删了哪一条」这类局部结果时不要拼一份去写穿
  ——拼的底稿可能是旧的，用 `invalidateAsyncData(key)`。
- **换人 / 结束会话**（由身份存储调用，页面不用管）：
  - `writeCachedIdentity` 发现 `accountId` 变了 → `clearAsyncDataCache()`：挂着的钩子丢数据并按新身份重取；
  - `clearCachedIdentity`（登出、被踢、改密完成）→ `clearAsyncDataCache({ refetch: false })`：
    页面马上离开，此刻重取只会带着空凭据撞 401。
- **编辑器的权威状态不给键**：用读取结果初始化草稿的地方（表单回填后自动保存等），拿缓存初始化会把旧值写回去；
  要么不给键，要么 `refreshing` 期间不接受写入。

## 在途 GET 合并（`platformRequest`）

- 只合并**纯 GET**：`init` 除 `method` 外没有任何键（无 body、signal、自定义头）。带 signal 的请求各有超时，
  合并后一个调用方的 abort 会连累别人。
- 键 = `[path, token, preserveSessionOn401]`：换人或 401 口径不同都不合并。
- 查找与登记在第一个 await 之前同步完成；请求落定即出表，之后的调用重新发请求（不是缓存）。
- 被合并过的结果每个调用方各拿一份 `structuredClone`；失败共享同一个 `PlatformRequestError`。
- 非 GET 与其余带选项的请求照旧直发。

## 模板示例

`app/[locale]/app/notifications/page.tsx`：`useAsyncData(loadNotifications, canView, { cacheKey: "/notifications?limit=100" })`，
点掉通知先乐观隐藏，成功后 `invalidateAsyncData(NOTIFICATIONS_KEY)`（作废点之前发出的复查、后台重取），失败 `reload()`。

## 宿主接入清单

- 整份复制 `lib/async-data-cache.ts`、`lib/use-async-data.ts` 及两份测试；`lib/platform-api.ts` 对照合入
  `inFlightGets` 一段；`lib/identity-cache.ts` 加上面两处 `clearAsyncDataCache` 调用。
- 业务页按资源逐个给 `cacheKey`，键从本宿主的路径构造函数取，不要手写第二份。
- **已有更强缓存的宿主不接**：EasyCustoms 的 `useEnvelopeQuery`（`lib/query-cache.ts`）自带账号隔离与单飞，
  不要再叠一层 `useAsyncData`。
- 回归用例：`lib/use-async-data.test.tsx`（命中 / 换键 / 过期响应 / 事件 / 401-403-404 与 5xx）、
  `lib/platform-api.test.ts`（合并与不合并的边界）、`lib/identity-cache.test.ts`「async data cache follows the identity」。
