"use client";

/**
 * ⚠️ 模板示例,复制成新宿主后请删掉整个 `components/examples/` 与
 * `app/[locale]/app/examples/`(以及 `lib/messages.ts` 的 `examples` 文案块和
 * `components/blank-shell.tsx` 里的 examples 导航分组)。
 *
 * 它只为演示共享的列表页约定,不是业务代码:
 *   - 一张表只有一份查询状态,住在地址栏(深链、刷新、前进后退还原同一屏);
 *   - 桌面上搜索 / 筛选 / 排序全部在表头,表格上方不放工具条(手机上没有表头,同一批
 *     能力由卡片列表搬到卡片上方的工具条里,宿主不用另写);
 *   - 分页走服务端,`table.listParams` 直接喂给列表接口;
 *   - 手机(< md)上同一份列定义自动换成卡片列表:标题列标 `mobile: "title"`,
 *     卡片工具条的四句文案走 `labels.cards`(见 `exampleColumns` 与 docs/SHELL_MOBILE.md)。
 *
 * 真实页面与这里唯一的区别:`page` 来自接口而不是内存里的假数据——
 * `useEffect(() => { void load(table.listParams); }, [table.listParams])`。
 * API 见 EasyUI `src/README.md` 的「表格」一节。
 */

import { InlineNotice, PageHeader } from "@easy-enterprise/ui";
import { DataTable, filterColumn, searchColumn, sortColumn, withEllipsis } from "@easy-enterprise/ui/table";
import { useMemo } from "react";
import { useTableQuery, type Page, type TableQueryConfig, type TableQueryState } from "../../lib/table-query";
import { localeOf, messages, type Locale } from "../../lib/messages";

interface ExampleRow {
  id: string;
  name: string;
  status: ExampleStatus;
  owner: string;
  updatedAt: string;
}

type ExampleStatus = (typeof STATUSES)[number];
const STATUSES = ["active", "paused", "archived"] as const;

/**
 * 配置必须是模块常量(或 `useMemo`):返回的 `TableQuery` 记忆化在它上面,列又记忆化在
 * `TableQuery` 上,配置每渲染新建一次 antd 就会当成换了一张表,把用户刚打开的漏斗关掉。
 * `sortKeys` / `filterOptions` 是后端白名单:旧书签里的野值在发请求前就被丢掉。
 */
const CONFIG = {
  keys: ["q", "status"],
  sortKeys: ["name", "updatedAt"],
  filterOptions: { status: STATUSES },
  defaults: { sort: { key: "updatedAt", order: "desc" } },
} as const satisfies TableQueryConfig;

const OWNERS = ["林岚 / Lin Lan", "周正 / Zhou Zheng", "Ada Lovelace", "Grace Hopper"];

/** 30 行假数据;真实页面这里什么都没有,数据来自接口。 */
const ROWS: readonly ExampleRow[] = Array.from({ length: 30 }, (_, index) => ({
  id: `row-${index + 1}`,
  name: `${index % 2 === 0 ? "Sample record" : "示例记录"} ${String(index + 1).padStart(2, "0")}`,
  status: STATUSES[index % STATUSES.length],
  owner: OWNERS[index % OWNERS.length],
  updatedAt: `2026-0${(index % 9) + 1}-${String((index % 28) + 1).padStart(2, "0")}`,
}));

export function TableExample({ locale: rawLocale }: { locale: string }) {
  const locale: Locale = localeOf(rawLocale);
  const t = useMemo(() => messages(locale), [locale]);
  const table = useTableQuery(CONFIG);
  const page = useMemo(() => fakePage(table.listParams), [table.listParams]);

  const columns = useMemo(() => exampleColumns(t, table.query), [t, table.query]);

  return (
    <div data-test-id="examples-table-page">
      <PageHeader eyebrow={t.examples.navLabel} title={t.examples.title} subtitle={t.examples.subtitle} />
      <InlineNotice tone="info" message={t.examples.notice} data-test-id="examples-table-notice" />
      <div className="mt-4">
        <DataTable<ExampleRow>
          testId="examples-table"
          rowKey="id"
          columns={columns}
          page={page}
          query={table}
          labels={t.examples.table}
        />
      </div>
    </div>
  );
}

/**
 * 列定义(抽成纯函数,单测直接钉住列的形状,不用挂整张表)。
 *
 * 列上的 `mobile` 标记是手机卡片列表(`< md` 自动切换)读的:
 *   - `mobile: "title"` —— 这一列当卡片的标题行。不标就取排序后的第一列,而那未必还是
 *     那个「名字」,所以有名字列的表一律显式标上;
 *   - `mobile: "hidden"` —— 这一列在手机上整行不出现。留给桌面上有位置、手机上只是噪声的列
 *     (ID / 编码、创建人、以及已经有另一个日期时的创建时间);一张卡片的定义表 ≤ 4 行才读得动。
 * 标记写在最里层的列字面量上,一路穿过 `searchColumn` / `withEllipsis` / `sortColumn`
 * 这些装饰器(它们收发的都是 `MobileColumn`);桌面表格对它完全无感。
 *
 * 这张示例表只有四列,标题行之外正好三行定义表,所以没有该藏的列。
 */
export function exampleColumns(t: ReturnType<typeof messages>, query: TableQueryState) {
  return [
    sortColumn(
      withEllipsis(
        searchColumn<ExampleRow>(
          { title: t.examples.columns.name, dataIndex: "name", mobile: "title" },
          { param: "q", query, labels: t.examples.table, placeholder: t.examples.columns.name },
        ),
        260,
      ),
      "name",
      query,
    ),
    filterColumn<ExampleRow>(
      {
        title: t.examples.columns.status,
        dataIndex: "status",
        width: 140,
        render: (_value: unknown, row: ExampleRow) => t.examples.status[row.status],
      },
      { param: "status", query, options: statusOptions(t), labels: t.examples.table },
    ),
    withEllipsis<ExampleRow>({ title: t.examples.columns.owner, dataIndex: "owner" }, 200),
    sortColumn<ExampleRow>({ title: t.examples.columns.updatedAt, width: 160 }, "updatedAt", query),
  ];
}

function statusOptions(t: ReturnType<typeof messages>) {
  return STATUSES.map((value) => ({ text: t.examples.status[value], value }));
}

/**
 * 内存版的「列表接口」:只为让示例点得动。
 * 真实宿主把同一份 `listParams` 交给 `lib/platform-api.ts` 的列表函数。
 */
function fakePage(params: Record<string, string | number | boolean | undefined>): Page<ExampleRow> {
  const keyword = String(params.q ?? "").toLowerCase();
  const statuses = String(params.status ?? "").split(",").filter(Boolean);
  const [sortKey = "updatedAt", direction = "desc"] = String(params.sort ?? "").split(":");
  const page = Number(params.page ?? 1);
  const pageSize = Number(params.pageSize ?? 20);
  const matched = ROWS.filter(
    (row) =>
      (!keyword || row.name.toLowerCase().includes(keyword)) &&
      (statuses.length === 0 || statuses.includes(row.status)),
  );
  const sorted = [...matched].sort((left, right) => {
    const compared = sortKey === "name" ? left.name.localeCompare(right.name) : left.updatedAt.localeCompare(right.updatedAt);
    return direction === "asc" ? compared : -compared;
  });
  return {
    items: sorted.slice((page - 1) * pageSize, page * pageSize),
    page,
    pageSize,
    total: sorted.length,
  };
}
