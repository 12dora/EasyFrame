# 外壳的手机形态（mobile shell）

手机上原来是两条头部栏：`Topbar` 57 px + `MobileNav` 分区栏 57 px（EasyUI 当时的顶栏高度），正文要到 114 px 之后才开始；
框架底部还钉着一条 ~50 px 的页脚，列表又是横向滚动的表格 —— 390×844 的屏幕一屏只剩四行。
本文是 blank 模板里手机形态的契约与宿主镜像清单，配套的是
[SHELL_NAV_INTENT.md](./SHELL_NAV_INTENT.md)（点侧栏多久有反应）与
[SHELL_PERCEIVED_LOADING.md](./SHELL_PERCEIVED_LOADING.md)（第一屏多久出来）。

组件侧的改动全部住在 EasyUI（`frontend/packages/easy-enterprise`）里，见它的
`src/README.md` 的「移动端」与「移动端卡片列表」两节；本文只写**宿主要做什么**。

## 口径

**手机 = Tailwind 的 `md` 以下（< 768 px）。** 所有适配一律走 `max-md:` / `md:` 断点，或
`(max-width: 767px)` 媒体查询钩子（EasyUI 的 `useIsPhone()`，宿主也可以留自己的
`useIsNarrow()`）。**`md` 及以上一个像素都不许变** —— 改的永远是响应式对子里的手机那一半。
回归时的判据就是这一条：桌面截图与改前逐像素相同。

## 宿主清单

### 1. 一条头部栏：汉堡进 `Topbar` 的 `leading` 槽

`MobileNav` 有 `variant`：`"bar"`（默认，旧行为，带分区标题的独立顶栏）和 `"trigger"`
（只渲染汉堡按钮，自带 `md:hidden`，加抽屉 portal）。宿主改成后者，并且**不再给
`EnterpriseAppFrame` 传 `mobileNav`**：

```tsx
const mobileNav = (
  <MobileNav
    variant="trigger"
    model={model}
    renderLink={renderNavLink}
    backLabel={t.navigation.backToMain}
    menuLabel={t.navigation.menu}
    closeLabel={t.navigation.close}
    navLabel={t.navigation.menu}
    /* 抽屉靠 pathKey 关闭 —— 必须是真实 pathname，不是 nav.path，见 SHELL_NAV_INTENT.md */
    pathKey={pathname}
    footer={drawerFooter}
  />
);

<EnterpriseAppFrame
  topbar={<ShellTopbar … leading={mobileNav} />}
  sidebar={<Sidebar … />}
  /* 不再传 mobileNav */
  footer={footer}
  mainClassName="pb-6 md:pb-12"
>
```

顶栏组件要把 `leading` 透到 `Topbar` 上（模板里 `ShellTopbar` 多收一个可选
`leading?: ReactNode`）。两种 variant 共用同一份抽屉实现，所以下钻、`pathKey` 关闭并重置、
焦点陷阱、Esc、断点关闭这些行为一个都没变。

**不是每个顶栏都要带 `leading`。** 模板里强制改密壳（`EnterprisePublicShell`）那一份就不带：
那一页只能改密码，没有导航模型可进。

### 2. 页脚要传两遍

`AppShell` 的页脚包裹层现在是 `hidden md:block`：手机上页面底部不再钉页脚，那份文案改由导航
抽屉的底部承载。所以传了框架 `footer` 的宿主**必须**把同一份内容再交给 `MobileNav footer`：

```tsx
const footerHtml = resolveEnterpriseFooterHtml(settings, locale);
const footerFallback = <>{t.public.footer} · © {new Date().getFullYear()}</>;
// 页面底部那一份：带 <footer> 地标（app-footer-html / app-footer-fallback）
const footer = <EnterpriseConfiguredFooter html={footerHtml} fallback={footerFallback} />;
// 抽屉里那一份：bare（app-footer-html-inline / app-footer-fallback-inline）
// 全局「显示页脚」关掉时不给：MobileNav 拿不到 footer 就连那条 border-t 区域都不画
const showFooter = resolveEnterpriseShowFooter(settings);
const drawerFooter = showFooter ? <EnterpriseConfiguredFooter bare html={footerHtml} fallback={footerFallback} /> : undefined;
// 框架那一份照传，另给 EnterpriseAppFrame showFooter={showFooter}（见 GENERAL_SETTINGS.md）
```

**抽屉里那份一定要 `bare`。** 抽屉本身是 `role="dialog"`，再嵌一个 `<footer>` 会在对话框里多出
一个 contentinfo 地标（读屏会把它当成页面级页脚播报），边框也会跟抽屉自己的分隔线叠成双线。

漏了 `MobileNav footer` 的症状是「手机上页脚整条消失」，而且桌面完全正常 —— 所以单测要两份都钉。

### 3. 正文底部留白 `pb-6 md:pb-12`

`mainClassName` 从 `pb-12` 改成 `pb-6 md:pb-12`：手机上少 24 px 的空滚动区。
`APP_SHELL_MAIN_PADDING` 的竖向留白（24 → 16）由 EasyUI 管，宿主不用动。

**宿主若有跟着外壳留白算的常量，要一起跟。** 典型的是吸顶详情头的占位高度
（`before:h-6 md:before:h-12` 之类镜像 `py-6 md:py-12` 的写法）：外壳的手机竖向留白变了，
这个常量不跟就会露出一条缝或压住内容。blank 模板里没有这类页面，宿主自己搜 `before:h-`。

### 4. 列表在手机上换成卡片

`ClientTable` / `DataTable` 在 `(max-width: 767px)` 下**默认**换成卡片列表（`mobile="table"`
可以显式退出）。同一份列定义两种形态共用，宿主只补两样东西：

**a. `labels.cards` 的四句文案。** `DataTableLabels.cards` 收 `Partial<TableCardsLabels>`：
`sort`（排序下拉）/ `all`（枚举筛选的「不限」）/ `selectAll`（全选本页）/ `select`（单行选择框
的读屏名字前缀）。缺省值是中文，**双语宿主不给英文那一份，英文界面上就会冒出中文**：

```ts
table: { search: "Search", …, cards: { sort: "Sort", all: "All", selectAll: "Select all on this page", select: "Select" } },
```

**b. 列上的 `mobile` 标记**（`MobileColumn` 的 `mobile?: "hidden" | "title"`）：

| 标记 | 含义 | 什么时候标 |
|---|---|---|
| `"title"` | 这一列当卡片的标题行 | 有「名字」列的表一律显式标。不标就取排序后的第一列，而换个默认排序标题就换人了 |
| `"hidden"` | 这一列在手机上整行不出现 | ID / 编码（已经有名字列时）、创建人、以及已经有另一个日期时的创建 / 更新时间 |

判据是**一张卡片的定义表 ≤ 4 行**（标题行之外）：再多就滚不动了。渲染结果为空的列本来就不出现，
不用为它标 `"hidden"`。标记写在**最里层的列字面量**上，一路穿过 `searchColumn` /
`filterColumn` / `sortColumn` / `withEllipsis`（它们收发的都是 `MobileColumn`）：

```ts
searchColumn<Row>({ title: t.columns.name, dataIndex: "name", mobile: "title" }, { param: "q", … })
```

**表头的检索 / 筛选 / 排序列不要删**：卡片工具条正是从它们推导出来的（每个 `searchColumn` 一个
输入框、`filterColumn` 一个下拉或一排芯片、可排序列合成一个「排序」下拉），走的还是同一条
`onFilters` / 查询 patch 通路，所以横竖屏切换不丢关键字与页码。

模板里的样板是 `frontend/apps/blank/components/examples/table-example.tsx`（列定义抽成了
纯函数 `exampleColumns`，单测直接钉列的形状，不用挂整张表）。

### 5. 表格之外的手机分支仍归宿主

EasyUI 只管外壳与表格。分栏详情页、并排的编辑面板、宽表单这类布局的手机分支还是宿主自己用
`useIsNarrow()` 一类的钩子写（`(max-width: 767px)`，与 `md` 同一个断点）。
**不要把 EasyUI 的 `useIsPhone()` 引到宿主业务代码里**，除非宿主没有自己的那一个：两个钩子
语义相同，混着用只会让「断点住在哪」变成两处事实。

## 单测清单

模板里分两个文件（mock 需求不同，合在一起会互相打架）：

| 文件 | 钉住什么 |
|---|---|
| `components/blank-shell.test.tsx` | 导航速度那一组（意图、预取、进度条）继续用替身；顶栏替身**必须渲染 `leading`**，`MobileNav` 替身透出 `variant` 与 `pathKey`，用例断言宿主要的是 `variant="trigger"` 且它在 `<header>` 里 |
| `components/blank-shell.mobile.test.tsx` | 用**真的** `Topbar` / `MobileNav` / `EnterpriseAppFrame`：汉堡在 `<header>` 里且带 `md:hidden`、`aria-label` 是本地化的「菜单」、`aria-haspopup="menu"`、点击前 `aria-expanded="false"`；`admin-mobile-nav` 分区栏不再渲染；`<main>` 有 `pb-6` 与 `md:pb-12` 而**没有**裸 `pb-12`；点汉堡打开抽屉后 `app-footer-fallback-inline` 在抽屉里、抽屉里**没有** `<footer>`；页面底部那一份 `<footer>` 的包裹层同时有 `hidden` 与 `md:block`；强制改密壳不渲染汉堡、页脚仍是带地标的那一份 |
| `components/examples/table-example.test.tsx` | 列定义那一层：名称列 `mobile: "title"`、标记穿得过整串装饰器、`labels.cards` 中英两套都给齐。再在手机视口下**真的挂一遍整页**（`matchMedia` 对 `(max-width: 767px)` 答 `true`）：`examples-table-cards` 出现、没有 `<table>` 也没有 `q-search-icon`、标题行是样例名称、`dt` 正好是其余三列、工具条的 `-cards-search-q` / `-cards-filter-status` / `-cards-sort` 与底部 `-cards-pagination` 都在、英文界面的排序下拉读屏名是 `Sort`、筛选首项是 `Status:All` |

**class 断言要按 token 集合比，不要 `toContain`。** `"pb-12"` 是 `"md:pb-12"` 的子串：子串断言分不出「手机上也留了 48 px」和「只有桌面留 48 px」，断点前缀写漏了照样绿。`hidden` / `md:block` 这一对同理 —— 少一个就分别是「手机上页脚没收起」与「桌面上页脚没了」。

**第二份 react 的坑。** EasyUI 的 `motion` 与 `antd` 都是 peer 依赖，装在
`packages/easy-enterprise/node_modules/.pnpm` 下、与**另一个** react 小版本配对，从它们自己
那层解析 react ⇒ 拿到第二份（`Cannot read properties of null (reading 'useState' /
'useRef')`）。`resolve.dedupe` 管不到 node_modules 里被 externalize 的依赖，所以
`apps/blank/vitest.config.ts` 里按实路径把 `react` / `react-dom` / `antd` 钉成宿主那一份，
另把 `motion` / `framer-motion` `server.deps.inline` 进来，让别名对它们内部的 react 也生效。
有了这几条，带动效的 kit 组件（`MobileNav` 的抽屉）与带 antd 的卡片列表（`Pagination` /
`Checkbox`）在宿主用例里都能用真的，不必换替身。

## 已知不变量（回归时照着看）

- **桌面（md+）逐像素不变**：`Topbar`、`AppShell` 留白、`PageHeader`、列表仍是表格。
- 手机上 `<main>` 之前只有一条头部栏（EasyUI `Topbar`，48 px 高 + 1 px 底边）；`admin-mobile-nav` / `admin-mobile-nav-current`
  在任何页面都不再出现。
- 手机上页面底部没有页脚，抽屉底部有；桌面反过来。两处文案是同一份（同一个 `footerHtml`），也跟同一个 `showFooter` 开关：关掉时两处都不画。
- `<main>` 的上内边距由 EasyUI 定义（手机 12 px / md 起 16 px，CSS 变量 `--app-shell-main-pt`，`@easy-enterprise/ui/shell` 导出 `APP_SHELL_MAIN_PADDING_TOP_PX` / `APP_SHELL_MAIN_PADDING_TOP_VAR`）。宿主里要跟它对齐的东西（吸顶页眉上方的挡板等）一律读这个变量（如 `before:h-[var(--app-shell-main-pt)]`），不要抄一对断点值或写死顶栏高度。
- 抽屉仍在**路由真的变化**时才关（`pathKey` 是真实 `pathname`）；Esc、点遮罩、断点回桌面都能关。
- 手机上列表是卡片、没有横向滚动条；转回桌面，刚才在卡片工具条里选的关键字 / 筛选 / 排序 / 页码
  一个不丢（它们本来就住在同一份查询状态里）。
- SSR / hydration 首帧是桌面形态（`useIsPhone()` 的服务端快照恒为 `false`），手机在 hydration
  之后的第一次订阅里切成卡片，`console` 无 hydration 不匹配。
