# 侧栏即时反馈：导航意图（nav intent）

点侧栏之后「什么都没发生」的那 300~1500 ms，是 EasyFrame 家族所有站点共有的一条体感缺陷。
本文是 blank 模板里这条链路的契约与宿主镜像清单，配套的是
[SHELL_PERCEIVED_LOADING.md](./SHELL_PERCEIVED_LOADING.md)（首屏身份）——那篇管「第一屏多久出来」，
这篇管「点下去多久有反应」。

## 问题

侧栏的选中标记一直是从 `usePathname()` 算的。在 Next.js 16 App Router 里，`usePathname()`
**只在导航提交之后才翻页**：外壳段是 `force-dynamic`、route 级 `loading.tsx` 被刻意删掉、
侧栏链接只按悬停意图预取。于是远端 / 高 RTT 访问下的时序是：

```
点击 → （等新路由的 RSC 载荷）300~1500ms → usePathname() 翻页 → 侧栏标记与页面内容同时换掉
```

这段空窗里屏幕上一个像素都没动，用户的判断是「侧栏不跟手」，然后再点一次。

要的效果是两件事分开：

- **侧栏同步响应点击**（标记滑过去、字重变粗、面板展开）——不等路由；
- **内容列出现一条克制的、延迟出现的进度轨**——告诉用户它在路上。

内容本身仍然保持上一页不动（App Router 的默认行为）。**不要为此重新引入 route 级 `loading.tsx`
或外壳内的 `<Suspense fallback>`**：React 的 ~300 ms Suspense 揭示节流会让每次导航都至少挂一段骨架屏，
反而更慢（见 SHELL_PERCEIVED_LOADING.md 的「导航速度」一节）。

## EasyUI 的 API（`@easy-enterprise/ui/shell`）

```ts
export const NAV_INTENT_TIMEOUT_MS = 8000;
export function useNavIntent(pathname: string): NavIntent;

export interface NavIntent {
  /** 用来算 `active` 的路径：有意图未落地时是意图路径，否则就是真实 `pathname`。 */
  path: string;
  /** 从 `onIntent(href)` 起，到 `pathname` 发生任何变化（或超时）为止为真。 */
  pending: boolean;
  /** 在导航链接的 click 处理里同步调用（面板项则在 `router.push` 之前调用）。 */
  onIntent: (href: string) => void;
}
```

行为约定（`src/shell/nav-intent.ts`）：

- `onIntent(href)` 只存 href 的**路径部分**（丢掉 `?query` 与 `#hash`），不做 locale 改写
  ——宿主传进来的 href 本来就是本地化过的。
- **点当前页是空操作，而且会撤掉未落地的旧意图**：目标路径等于当前 `pathname` 时不产生 pending
  （点自己不该转圈）；如果此时还有一次没落地的意图挂着，它也一并作废——**最后一次点击说了算**，
  那次导航的目标已经不是它了。
- `pending` 的清除条件：`pathname` 变成任何不同于「记意图那一刻」的值（导航落地了，
  也可能落在重定向目标上），或者 `NAV_INTENT_TIMEOUT_MS = 8000` ms 超时（导航被中止 / 出错，
  标记弹回真实路由）。后退 / 前进会改 `pathname`，因此不需要额外监听 `popstate`。
- pending 期间再点一次：直接换目标（重新记录 `pathname`、重置定时器）。
- **不会造成水合不一致**：初始 state 只由 `pathname` 决定；`pathname` 的追平走「渲染期间调整 state」，
  不是 `useEffect` + `setState`（否则会多出一帧旧标记，而且陈旧意图会在用户后退时被「复活」）。
- 纯逻辑、与路由库无关（EasyUI 不引 `next/*`），宿主把自己的 `usePathname()` 传进来。

`AppShell` / `EnterpriseAppFrame` 多了两个 prop：

```ts
/** 有一次客户端导航在途：内容列顶边显示延迟出现的进度条，`<main>` 挂 `aria-busy`。 */
pending?: boolean;
/** 进度条状态文案（本地化，视觉隐藏，只读给读屏）。 */
pendingLabel?: string;
```

进度条本体是 `src/shell/NavigationProgress.tsx`：内容列顶边 2px 琥珀色细轨，**pending 满
`NAV_PROGRESS_DELAY_MS = 150` ms 才显形**（秒开的导航永远不闪），ease-out 的不确定进度动效，
pending 落下时补满 + 淡出（~200 ms），动效全走 `theme.css` 的关键帧（shell 关键路径不引
`motion/react`，见 FE-PERF-04），`prefers-reduced-motion` 下退化成纯显示 / 隐藏。

## 宿主接线清单（模板里就是 `components/blank-shell.tsx` 这几行）

1. **取意图**，紧跟在 `usePathname()` 之后：

   ```ts
   const pathname = usePathname();
   const nav = useNavIntent(pathname);
   const inSettings = nav.path.includes("/settings/");
   ```

2. **一切「选中态」改读 `nav.path`**：

   ```ts
   const active = useCallback((path: string) => nav.path === href(path), [href, nav.path]);
   const activePrefix = useCallback((path: string) => nav.path === href(path) || nav.path.startsWith(`${href(path)}/`), [href, nav.path]);
   ```

3. **面板开合也改读 `nav.path`**（模板里是 `/settings/` 那一套，共四处）：
   `useState` 初值、`wasInSettings` 的 ref 初值、自动展开的 effect（依赖从 `pathname` 改成 `inSettings`）、
   `openPanelId`；导航模型里面板的 `active` 同样改成 `inSettings`。

4. **链接的 click 里先记意图，再跑 `onNavigate()`**。模板的 `renderNavLink` 是模块级常量
   （传给侧栏的 prop 身份要稳定），捞不到外壳闭包，于是加一层 context：

   ```tsx
   const NavIntentContext = createContext<(href: string) => void>(() => undefined);
   // NavLinkWithIntentPrefetch 内：
   const onIntent = useContext(NavIntentContext);
   const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
     const plain = event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !event.defaultPrevented;
     if (plain) onIntent(href);
     onNavigate();
   };
   ```

   宿主也可以改成在外壳里用 `useCallback` 造 `renderNavLink`，两种都行；**顺序不能换**
   （`onNavigate()` 会关移动端抽屉等，先记意图才保证标记在同一帧里挪好）。
   悬停 / 聚焦 / 触摸预取的行为保持不变。

   **只有普通左键点击才记意图**：带修饰键（⌘/Ctrl/Shift/Alt）、中键、`target` 不是 `_self`、
   以及已被 `preventDefault()` 的点击，Next 的 `Link` **照样会调用宿主的 `onClick`，然后不导航**
   （新标签页打开 / 交给浏览器默认行为）。这些情况下 `pathname` 永远不变，记了意图就会让标记在
   一个用户根本没离开的条目上停满 `NAV_INTENT_TIMEOUT_MS = 8000` ms。`onNavigate()` 仍要照常调用。

5. **面板入口（按钮，不是链接）在 `router.push` 之前记意图**：

   ```ts
   const openPanel = (next: NavPanel) => { nav.onIntent(next.firstHref); setPanel(next.id); router.push(next.firstHref); };
   ```

6. **框架喂 pending**：

   ```tsx
   <EnterpriseAppFrame pending={nav.pending} pendingLabel={t.common.loading} …>
   ```

   模板复用共享文案目录里的 `common.loading`（「正在加载」/「Loading」）；宿主若没有同类 key，
   在自己的 `lib/messages.ts` 里按 `shell` 分组补一条「加载中」，两种语言都要。

### 必须继续用真实 `pathname` 的地方

**只有「画选中态」这件事挪到 `nav.path`**，其余一概不动：

- `MobileNav` 的 `pathKey`（抽屉要在路由**真的**变了才关；换成 `nav.path` 会在点击瞬间关掉，
  用户看不到标记挪动，反而更像掉帧）；
- 顶栏 `EnterpriseTopbarActions` 的 `pathKey`；
- 身份钩子 `useShellIdentity({ pathname, … })`、强制改密页的 `pathname === forcedTarget` 判定；
- 语言切换（`localizedLocation(pathname, …)`）；
- **任何会发请求的东西**（导航后的例行复查、列表拉取）。意图只是画面，不是事实。

## 宿主镜像清单

### EasyUI（`@easy-enterprise/ui`，已完成）

| 文件 | 改动 |
|---|---|
| `src/shell/nav-intent.ts` | **新增**：`useNavIntent` / `intentPathOf` / `NAV_INTENT_TIMEOUT_MS` |
| `src/shell/NavigationProgress.tsx` | **新增**：延迟 150 ms 的顶边进度轨 |
| `src/shell/AppShell.tsx` | 加 `pending` / `pendingLabel`；`<main>` 外包一层 `relative` 容器挂进度条，`<main>` 挂 `aria-busy` |
| `src/theme.css` | `easyNavProgress*` 关键帧（挨着既有的 `easy-*` 动画） |
| `src/shell/index.ts` | 导出上述 API |

`EnterpriseAppFrame` 是 `Omit<AppShellProps, "footer">`，两个新 prop 自动透传，无需另改。

### 每个宿主（EasyTrade 主站 / EasyCustoms / EasyLearning）

| 文件 | 改动 |
|---|---|
| `components/<host>-shell.tsx` | 上面 1~6 全套；`active` / `activePrefix` / 面板开合的 `pathname` 全换成 `nav.path` |
| 同上（若有第二个外壳） | EasyLearning 的 `TakingFrame` 之类**共用侧栏的外壳一并接**，不要只改主外壳 |
| `lib/messages.ts` | 没有「加载中」类文案时按 `shell` 分组补一条，中英各一 |
| `components/<host>-shell.tsx`（链接 onClick） | **只在普通左键点击时记意图**（`plain` 判定见上）；`onNavigate()` 无条件调用 |
| `components/<host>-shell.test.tsx` | 补：点击后在 `usePathname()` 翻页之前 `aria-current="page"` 已挪过去；pending 期间 `<main>` 有 `aria-busy`、`[data-test-id="nav-progress"]` 过了 `NAV_PROGRESS_DELAY_MS` 才 `role="status"`；mock 的 pathname 更新后 pending 清除并回 `idle`；**ctrl 点击 / 中键点击不动 `aria-current` 与 `aria-busy`**；**点当前页既不转圈也会撤掉未落地的旧意图**；面板展开与其首项选中；落在非点击目标的路径上时意图让位；`NAV_INTENT_TIMEOUT_MS` 超时后标记弹回 |
| 单测替身 | **不要替换 `EnterpriseAppFrame`**（`AppShell` / `NavigationProgress` 不引 `motion/react`，用真的才验得到 `aria-busy` 与进度条）；`Sidebar` 替身要透出 `openPanelId` 并在展开时渲染 `panel.items`，`MobileNav` 替身要透出 `pathKey` |
| `package.json` / pin | `@easy-enterprise/ui` 的 submodule pin 跟到含 `nav-intent.ts` 的提交 |

宿主自己要当心的几处：

- **侧栏 mock 别漏 `onNavigate`**：单测里替身 `Sidebar` 调 `renderLink(...)` 时必须带上
  `onNavigate`，否则新的 `onClick` 会在 `onNavigate()` 上抛。
- **别把「记意图」提到 `onClick` 之外**（例如 `onPointerDown`）：那样连右键菜单、拖拽都会记一次。
- **宿主自有的「当前分组」判定**（EasyTrade 的多级面板、EasyCustoms 的 companies 前缀）也属于选中态，
  同样改读 `nav.path`；漏一处就会出现「链接亮了但父级面板没跟着展开」。
- **外链 / 跨根布局的入口**（整页加载，不是客户端导航）不要记意图：`pathname` 永不变化，
  标记会停在目标上直到 8 s 超时。
- **不要顺手给外壳补 `loading.tsx` 或 `<Suspense>`**：这次改动的前提就是没有它们。

### 已知不变量（回归时照着看）

- SSR / hydration 首帧与改前一致，`console` 无 hydration 不匹配。
- 点当前页：没有进度条、没有 `aria-busy`、标记不动；若当时还有未落地的意图，它被撤销（最后一次点击说了算）。
- ⌘/Ctrl/Shift/Alt 点击与中键点击（新标签页打开）：`aria-current` 与 `aria-busy` 一动不动。
- 导航失败 / 被中止：最多 8 s 后标记自己弹回真实路由。
- 秒开（已预取）的导航：进度条一次都不闪（150 ms 门槛）。
- 移动端抽屉仍在路由真的变化时才关。
