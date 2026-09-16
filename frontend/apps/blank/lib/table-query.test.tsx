// @vitest-environment happy-dom
/**
 * 用户风险:列表页的搜索 / 筛选 / 排序全靠这层适配把状态镜像进地址栏。它一坏,
 * 深链和前进后退就还原不出同一屏结果,表头也会和地址栏对不上。
 *
 * 这里只测宿主自己写的那几行(pathname / search / replace 的接线),
 * 查询状态本身的行为由 EasyUI 的 `use-table-query.behavior.test.tsx` 覆盖。
 */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const PATHNAME = "/zh-CN/app/examples/table";
let currentSearch = "";
const replaced: string[] = [];

vi.mock("next/navigation", () => ({
  usePathname: () => PATHNAME,
  useSearchParams: () => new URLSearchParams(currentSearch),
  useRouter: () => ({ replace: (href: string) => replaced.push(href) }),
}));

const { useTableQuery } = await import("./table-query");
import type { TableQuery, TableQueryConfig } from "./table-query";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CONFIG: TableQueryConfig = {
  keys: ["q", "status"],
  sortKeys: ["name", "updatedAt"],
  filterOptions: { status: ["active", "paused", "archived"] },
  defaults: { sort: { key: "updatedAt", order: "desc" } },
};

let table: TableQuery | null = null;
let host: HTMLDivElement | null = null;
let root: ReturnType<typeof createRoot> | null = null;

function Harness() {
  table = useTableQuery(CONFIG);
  return null;
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Harness />));
}

beforeEach(() => {
  currentSearch = "";
  replaced.length = 0;
  table = null;
});

afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("useTableQuery", () => {
  it("reads the current state out of the address bar", async () => {
    currentSearch = "q=alpha&status=paused&sort=name:asc&page=2";
    await mount();
    expect(table?.query.q).toBe("alpha");
    expect(table?.query.filters.status).toEqual(["paused"]);
    expect(table?.query.sort).toEqual({ key: "name", order: "asc" });
    expect(table?.query.page).toBe(2);
    expect(table?.filtered).toBe(true);
  });

  it("mirrors a header action back into the address bar, keeping foreign parameters", async () => {
    currentSearch = "tab=overview";
    await mount();
    await act(async () => table?.setSearch("q", "beta"));
    expect(replaced).toEqual([`${PATHNAME}?tab=overview&q=beta`]);
  });

  it("keeps the default sort out of the query string but in the state", async () => {
    await mount();
    expect(table?.query.sort).toEqual({ key: "updatedAt", order: "desc" });
    await act(async () => table?.setSort("updatedAt", "desc"));
    expect(replaced).toEqual([PATHNAME]);
  });

  it("drops a sort key the endpoint does not accept", async () => {
    currentSearch = "sort=secret:asc";
    await mount();
    expect(table?.query.sort).toEqual({ key: "updatedAt", order: "desc" });
  });

  it("hands the same object back while the address bar does not change", async () => {
    await mount();
    const first = table;
    await act(async () => root!.render(<Harness />));
    expect(table).toBe(first);
  });
});
