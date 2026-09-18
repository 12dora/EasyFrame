# 外壳身份的感知加载（首屏）

同一个标签页里翻到第二页时，用户看到的是骨架屏还是页面，取决于**外壳身份什么时候落地**——
`components/blank-shell.tsx` 在拿到身份之前不挂 `children`，页面的第一条列表请求因此一条都还没发出。

本文是 blank 模板里这条链路的契约与宿主镜像清单。源头改动来自 EasyLearning
（`perf(shell)` 6d74cf9 + `fix(review)` e53fcd0），已回流到模板。

## 改前的时序

```
SSR 骨架屏 → hydration → await Promise.all([GET /auth/me, GET /auth/session]) → 外壳 + 页面 → GET /<列表>
```

即**列表请求的起跑线 = max(T_me, T_session)**。`/auth/session` 只提供一个「权限申请入口」，
却由 `SESSION_TIMEOUT_MS = 5000` 封顶——它慢一秒，所有表格就晚一秒开始；最坏 +5s 纯白屏。
另外每一次客户端导航都会重发这两条请求，和新页面的列表请求同时在途。

## 改后的三件事

### 1. 只等 `/auth/me`（`lib/shell-adapter.ts`）

`startShellIdentityLoad(fallbackName, identityLabels)` 把两条请求放在同一个 tick 发出，只 `await`
`/auth/me`，交回一个**永不 reject** 的 `session: Promise<ShellSession>`（失败 / 超时都回缺省值）。
身份里的 `permissionRequestUrl` 先给 `null`、`tableDensity` 先给缺省档，由调用方在后台补
（行高偏好见 [TABLE_DENSITY.md](TABLE_DENSITY.md)）。

`loadShellIdentity(fallbackName, identityLabels)` 保留为薄封装（= 新 API + `await session`），
签名与返回类型不变，既有调用方与契约测试不受影响。

结果：**列表请求的起跑线从 `max(T_me, T_session)` 降到 `T_me`**。

### 2. 本标签页身份快照（`lib/identity-cache.ts`，新增）

外壳身份的**外部存储**（`useSyncExternalStore` 的那个 store）：内存那份是当前身份，
sessionStorage 只是给下一次页面加载留的底稿。

- **不会 hydration 不匹配**：`getServerSnapshot` 恒为 `null` → SSR 与 hydration 首帧仍是骨架屏，
  hydration 完成后 React 才按快照重画。也因此不需要「effect 里 setState」。
- **对账**（`reconcileIdentity`）：`accountId` 相同 → 用真身份；不同 → 整份替换重画。
  `/auth/session` 还在途时沿用快照里的申请入口与行高档位，引导页按钮不会先消失再出现，
  选了「宽松」的人也不会每次刷新都先看一眼紧凑的表格；
  一旦 `/auth/session` 落地（第三个参数 `sessionSettled = true`），**它的答案即权威，`null` 也算**
  ——否则一个已被取消配置的申请地址会靠快照在同标签页每次刷新时复活。
- **等价即不动**（`sameIdentity`）：导航后的例行复查不该让整棵树重渲染。
- **安全边界**：只进 sessionStorage（随标签页结束、不跨标签页），按 `locale` 分档（身份行与兜底名
  是本地化文案），字段不合契约就整份作废，**强制改密的身份只活在内存、绝不落盘**，
  `try/catch` 包住所有存储读写（隐私模式会抛）。快照只影响画面，权限判定仍在服务端。
- `scheduleWhenIdle(run)` / `IDLE_TIMEOUT_MS = 1200`：把「可以晚一点做」的事排到
  `requestIdleCallback`（无该 API 时退化成定时器），返回取消函数。

### 3. 外壳钩子（`components/use-shell-identity.ts`，新增）

`useShellIdentity({ locale, pathname, fallbackName, identityLabels })` → `{ identity, permissionUrlPending, refreshIdentity }`。

- 身份状态读上面那个 store；快照在手就立刻出外壳与页面，真身份回来后台对账。
- **首屏与显式 `refreshIdentity()`（引导页的「重新检查」）立即取；导航后的例行复查排到空闲**
  （≤1.2s），连续导航自动取消合并。快照读不出来时（首屏、刚换语言）一律立即取——
  那时候屏幕上本来就是骨架屏，再让 1.2s 只是让空白多挂一会儿。
- **强制改密拦截不变**：除改密页外不给身份 + `router.replace` 过去；判定直接看已拿到的身份
  （`blocked`），不再依赖「每次导航重取 `/auth/me`」。被拦的账号照旧不落盘。
- **401 / 加载失败的处理不变**：清本地会话（`endLocalSession`）+ 回登录页；
  平台层的 `BLANK_AUTH_INVALIDATED_EVENT` 走同一条路。
- **跨标签页换人**：监听 `storage` 事件上的 `AUTH_TOKEN_STORAGE_KEY`，变了就清快照并整页重载
  ——否则这一页会一直画着上一个人的侧栏，直到下一次请求撞上 401。
- `onboardingReady(identity, permissionUrlPending)`：零授权引导页（**且只有这一页**）在申请入口未知
  且 `/auth/session` 仍在途时继续显示骨架屏——那一页的全部内容就是那个入口。
- `refreshIdentity()` 返回 `Promise<void>`，在这次重取落地时才 resolve：
  `EnterprisePermissionOnboarding` 靠 `await onRecheck()` 驱动「重新检查」按钮的忙态，
  返回 `void` 会让忙态在同一个微任务里就消失。

## 清快照的两个调用点（务必接对）

`clearCachedIdentity()` 必须在**显式结束会话**时调用，并且**绝不能塞进 `logout()` 本身**：
平台层拿到 401 也调 `logout()`，那一路正要靠内存里的身份把外壳留在屏幕上。

模板里的接法是 `lib/auth-adapter.ts` 的 `endLocalSession() { clearCachedIdentity(); logout(); }`，
接在这两处：

1. **登出适配器** — `enterpriseLogoutAdapter.clearLocalSession: endLocalSession`（`lib/auth-adapter.ts`）。
2. **改密成功回登录页** — `completeEnterprisePasswordChange({ clearLocalSession: endLocalSession, … })`。
   模板有**两个**这样的页面：`app/[locale]/app/settings/security/page.tsx`（普通改密）与
   `app/[locale]/app/settings/security/password/page.tsx`（强制改密），两个都要改。

外壳自己的踢人路径（`ejectToLogin`）也走 `endLocalSession`，已在钩子里接好。

`lib/identity-cache.test.ts` 的 `endLocalSession` 一组用例把这四条钉住了（包括「401 那一路的
`logout()` 不碰快照」），复制到宿主后不要删。

## 导航速度：外壳路由与外壳组件的约束

来源同为 EasyLearning（`perf(shell)` e04df7e、6ac94e6、94d7845），身份快照解决「首屏」，这几条解决「点侧栏」。

- **外壳内不加 `loading.tsx`**（`app/[locale]/app/**`）：React 19 的 Suspense 揭示节流会让每次侧栏导航
  至少挂约 300 ms 骨架屏，哪怕 RSC 与缓存数据几十毫秒就到。导航期间保留旧页，各页自己画加载态
  （`useAsyncData` 的 `loading` / 表格覆盖层）。
- **外壳内的 `page.tsx` 不包非 `null` 的 `<Suspense fallback>`**：页面级边界在客户端导航加载页面代码块时
  先提交骨架屏，吃同样的节流。公开路由（登录页）不受此限。
- **外壳段 `app/[locale]/app/layout.tsx` 设 `export const dynamic = "force-dynamic"`**：`useSearchParams`
  在服务端就拿到真实查询串、不退回客户端渲染，页面因此不需要 Suspense 边界。
- **业务树上不调 `headers()`**：`<html lang>` 由 `app/[locale]/layout.tsx` 按路由参数给出（`[locale]` 是根布局）。
  不带语言段的页面（`/`、`/login/oidc-complete`）放在 `app/(bare)/`，只有那个根布局读中间件写入的
  `x-enterprise-locale`。两个根布局之间跳转是整页加载，这几页本来就是整页进出。
- **侧栏链接按意图预取**（`components/blank-shell.tsx`）：`Link prefetch={false}`，`mouseenter` / `focus` /
  `touchstart` 时 `router.prefetch(href)`；设置入口是 EasyUI 的按钮，外层按 `data-test-id="blank-nav-settings"`
  事件委托预取它的第一页。
- **通知状态只在顶栏**（`components/use-shell-notifications.ts`）：`useShellNotifications` 只在 `ShellTopbar`
  里调用，加载态翻转不重画外壳、侧栏与页面；首次拉取排到 `scheduleWhenIdle`。
  传入 `accountId`：对账换了人或权限开关变化时清空、丢弃上一轮在途响应并重拉。
- **公开页读查询串要自带边界**：`app/[locale]/login/page.tsx` 用 `useSearchParams`，不在外壳段的
  `force-dynamic` 之下，必须包 `<Suspense fallback={<PageLoadingSkeleton/>}>`。
- **`next.config.ts`**：`experimental.optimizePackageImports: ["antd", "@easy-enterprise/ui", "dayjs"]`
  （宿主有其它大桶导出的包，如 `recharts`，一并加上）。

`components/blank-shell.test.tsx` 钉住预取与通知隔离，`components/use-shell-notifications.test.tsx` 钉住钩子本身。

## 宿主接入清单（EasyTrade / EasyCustoms / EasyLearning）

### 对照模板 diff 这些文件

| 文件 | 改动 |
|---|---|
| `lib/shell-adapter.ts` | `ShellIdentity` 加 `permissionRequestUrl: string \| null` 与 `tableDensity`；新增 `ShellSession` / `ShellIdentityLoad` / `startShellIdentityLoad` / 抽出 `toShellIdentity`；`loadShellIdentity` 改为薄封装 |
| `lib/identity-cache.ts` | **整份新增**，只改 `CACHE_KEY` 为本宿主前缀（模板：`blank.shell.identity`）；字段变动时 `CACHE_VERSION` +1（模板当前为 2，随 `tableDensity` 入快照而升） |
| `lib/auth-adapter.ts` | 导出 `AUTH_TOKEN_STORAGE_KEY`（跨标签页 `storage` 监听要按它过滤）；新增 `endLocalSession`；`clearLocalSession` 改指它 |
| `components/use-shell-identity.ts` | **整份新增**（宿主若已有同名钩子则按此重写）；`BLANK_AUTH_INVALIDATED_EVENT`、登录路径、强制改密路径按本宿主改 |
| `components/blank-shell.tsx` | 三个 effect（身份 / session / auth-invalidated）整体换成 `useShellIdentity`；`permissionRequestUrl` 改读 `identity.permissionRequestUrl`；引导页门禁改 `onboardingReady`；`onRecheck` 改 `refreshIdentity` |
| `app/…/settings/security/page.tsx`、`app/…/settings/security/password/page.tsx` | `clearLocalSession: logout` → `clearLocalSession: endLocalSession` |
| `app/layout.tsx` → `app/(bare)/layout.tsx`；`app/page.tsx`、`app/login/oidc-complete/page.tsx` 同搬进 `(bare)` | 唯一保留 `headers()` 的根布局，只服务无语言段页面 |
| `app/[locale]/layout.tsx` | 持有 `<html lang={localeOf(params.locale)}>` + `<body>` + `Toaster`，并 import `globals.css` |
| `app/[locale]/app/layout.tsx` | `export const dynamic = "force-dynamic"` |
| `app/[locale]/login/page.tsx` | 默认导出只包 `<Suspense fallback={<PageLoadingSkeleton/>}>`，读 `useSearchParams` 的登录控制器挪进子组件——根布局不再读 `headers()` 后登录页不再是请求期动态，没有边界时 `next build` 报缺 Suspense 或首帧丢掉 `?next=` / `oidc_error` |
| `components/use-shell-notifications.ts` | **整份新增**；`blank-shell.tsx` 删掉通知 state，改由 `ShellTopbar` 调用并传入 `identity.accountId`（同标签页换人时清空重拉） |
| `components/blank-shell.tsx`（导航） | `renderNavLink` 用 `prefetch={false}` + 意图预取；设置面板加 `testId` 与 `SettingsEntryPrefetch` |
| `next.config.ts` | `experimental.optimizePackageImports` |
| `vitest.config.ts` | `environment` 改 `happy-dom`（快照住在 sessionStorage，钩子用例要挂真实 React 根）；`include` 加 `components/**` |
| `lib/identity-cache.test.ts`、`components/use-shell-identity.test.tsx`、`lib/shell-adapter.test.ts` | 新增 / 补用例 |

### 宿主自己必须动的调用点

- **两处 `clearCachedIdentity`**（见上一节）：登出适配器的 `clearLocalSession`，以及**每一个**
  `completeEnterprisePasswordChange` 调用点。漏掉任何一个，下一个人登录时会先看到上一位的外壳与侧栏，
  页面的第一批请求也照着上一位发。
- **静默身份复查**（有 `useEnterpriseIdentityCheck` 接线的宿主，如 EasyLearning；blank 模板没有）：
  把 `enabled` 加一道 `armed` 门，用 `scheduleWhenIdle` 打开，首屏那圈隐藏 iframe 的 OIDC 重定向
  就不再和列表请求抢；**401 恢复不等这个窗口**（`revalidateSession` 要能提前打开并把这一次复查排队结算）。
- **凭据 key**：`AUTH_TOKEN_STORAGE_KEY` 必须等于本宿主真正写进 `localStorage` 的那个 key。
- **`CACHE_KEY` 每个宿主独立**，且 `CACHE_VERSION` 在 `ShellIdentity` 字段变动时 +1。
- **删掉外壳内的 `loading.tsx` 与页面级 `<Suspense fallback>`**：EasyCustoms 的 `app/[locale]/app/companies/**/loading.tsx`、
  EasyTrade 的 `admin/loading.tsx` 都是同一类；外壳段布局补 `force-dynamic` 后页面也不再需要 Suspense 边界。
  登录等公开页的 Suspense 骨架屏保留。
- **根布局别读 `headers()`**：确认 `<html>` 在 `[locale]` 布局里；无语言段的回调页（含 EasyLearning 的
  `login/oidc-silent`）归到 `(bare)` 根布局，`headers()` 只留那里。
- **侧栏预取与通知隔离**照上一节接；宿主自己的侧栏入口按钮同样要事件委托预取。
- **共用同一份身份加载的其它外壳**（EasyLearning 的 `TakingFrame`）跟着改用同一个钩子，不要另起一份。

### 与 EasyLearning 当前实现的三点差异（回流时一并修）

- **`refreshIdentity()` 返回 `Promise<void>`**（EasyLearning 返回 `void`）：见上。
- **`reconcileIdentity(cached, fresh, sessionSettled)` 的第三个参数**（EasyLearning 没有）：
  没有它时，一个被取消配置的 `permission_request_url` 会靠快照在同标签页每次刷新时复活
  ——`/auth/session` 回了 `null`，而对账规则一律沿用快照里的旧值。
- 另外 EasyLearning 的 `app/[locale]/(shell)/app/settings/security/page.tsx`（普通改密）
  仍接 `clearLocalSession: logout`，**漏了一个清快照的调用点**。

### 已知不变量（回归时照着看）

- SSR / hydration 首帧仍是骨架屏，`console` 无 hydration 不匹配。
- 强制改密账号：除改密页外拿不到身份，且 sessionStorage 里没有任何快照。
- 零授权引导页：申请入口未知时仍等 `/auth/session`；有业务权限的人不受影响。
- 401 与会话失效的落点、登录重定向的 `next=` 参数一字未变。

点下侧栏之后到路由提交之间的那段空窗（选中标记与内容列进度条）见 [SHELL_NAV_INTENT.md](./SHELL_NAV_INTENT.md)。
