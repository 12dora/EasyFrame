"use client";

/**
 * 列表页表头查询状态的 Next 适配层。
 *
 * 查询状态本身(解析 / 序列化 / 合并 / 列装饰器 / `DataTable`)全部住在 EasyUI 的
 * `@easy-enterprise/ui/table` 里——EasyUI 不 import 任何路由,所以宿主只补这一个
 * `TableHistory`。页面统一从这里 import,换路由时只改这一个文件。
 *
 * 用法见 `components/examples/table-example.tsx` 与 EasyUI `src/README.md` 的「表格」一节。
 * 两条硬约束:
 *   1. `config` 必须引用稳定(模块常量或 `useMemo`):返回的 `TableQuery` 记忆化在它上面,
 *      列又记忆化在 `TableQuery` 上,配置每渲染新建一次就等于每渲染换一张表(打开的漏斗会被关掉);
 *   2. 对话框一类的临时表格用 `useLocalTableQuery`,不要把它的筛选写进宿主页面的地址栏。
 */

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTableQueryWith, type TableQuery, type TableQueryConfig } from "@easy-enterprise/ui/table";

export function useTableQuery(config: TableQueryConfig): TableQuery {
  const pathname = usePathname();
  const router = useRouter();
  // 依赖查询串而不是 searchParams 对象:后者每次渲染都是新引用。
  const search = useSearchParams().toString();
  return useTableQueryWith(config, {
    pathname,
    search,
    // replace 而不是 push:一次筛选不该让后退键多按一下;不滚动到顶。
    replace: (href) => router.replace(href, { scroll: false }),
  });
}

export { useLocalTableQuery } from "@easy-enterprise/ui/table";
export type {
  ListParams,
  Page,
  TableQuery,
  TableQueryConfig,
  TableQueryDefaults,
  TableQueryPatch,
  TableQueryState,
  TableSort,
  TableSortOrder,
} from "@easy-enterprise/ui/table";
