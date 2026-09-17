import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MobileColumn } from "@easy-enterprise/ui/table";
import type { TableQueryState } from "../../lib/table-query";
import { messages } from "../../lib/messages";
import { TableExample, exampleColumns } from "./table-example";

/**
 * 示例表是新宿主抄的那份样板,所以手机形态的约定要钉住:
 *   - 名称列显式标了 `mobile: "title"`(不标就取排序后的第一列,换个默认排序标题就换人了);
 *   - 手机视口下整张表换成卡片列表,表头的检索 / 筛选 / 排序搬到卡片上方的工具条;
 *   - `labels.cards` 的四句文案两种语言都给齐(缺了就掉回 EasyUI 的中文缺省值,
 *     英文界面上会冒出中文)。
 */

vi.mock("next/navigation", () => ({
  usePathname: () => "/zh-CN/app/examples/table",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const QUERY: TableQueryState = { filters: {}, q: "", sort: { key: "updatedAt", order: "desc" }, page: 1, pageSize: 20 };

function columnsByKey(locale: "zh-CN" | "en") {
  const t = messages(locale);
  const map = new Map<string, MobileColumn<never>>();
  for (const column of exampleColumns(t, QUERY) as readonly MobileColumn<never>[]) {
    map.set(String(column.key ?? column.dataIndex), column);
  }
  return map;
}

describe("示例表的手机卡片标记", () => {
  it("名称列当卡片标题,其余列照常进定义表", () => {
    const columns = columnsByKey("zh-CN");
    // `searchColumn` 把 key 改成了查询参数名 `q`。
    expect(columns.get("q")?.mobile).toBe("title");
    expect(columns.get("status")?.mobile).toBeUndefined();
    expect(columns.get("owner")?.mobile).toBeUndefined();
    expect(columns.get("updatedAt")?.mobile).toBeUndefined();
    // 四列里只有一个标题,定义表三行 —— 够短,不需要 `mobile: "hidden"`。
    const titles = [...columns.values()].filter((column) => column.mobile === "title");
    expect(titles).toHaveLength(1);
  });

  it("标记穿得过 searchColumn / withEllipsis / sortColumn 这串装饰器", () => {
    const column = columnsByKey("zh-CN").get("q");
    expect(column?.ellipsis).toBe(true);
    expect(column?.sorter).toBe(true);
    expect(column?.filterDropdown).toBeTruthy();
    expect(column?.mobile).toBe("title");
  });
});

describe("示例表的卡片文案", () => {
  it("中英两套都给齐了 cards 的四句", () => {
    for (const locale of ["zh-CN", "en"] as const) {
      const cards = messages(locale).examples.table.cards;
      expect(Object.keys(cards).sort()).toEqual(["all", "select", "selectAll", "sort"]);
      for (const value of Object.values(cards)) expect(value.length).toBeGreaterThan(0);
    }
  });

  it("英文那份不落回中文缺省值", () => {
    const cards = messages("en").examples.table.cards;
    expect(cards.sort).toBe("Sort");
    expect(cards.all).toBe("All");
    expect(cards.selectAll).toBe("Select all on this page");
    expect(cards.select).toBe("Select");
  });
});

// —— 手机视口下真的挂一遍整页,验的是「同一份列定义换了一种形态」这件事本身 ——

const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
let container: HTMLDivElement;
let root: Root;

/** `useIsPhone()` 读的是 `matchMedia("(max-width: 767px)").matches`。 */
function stubViewport(phone: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: query === "(max-width: 767px)" ? phone : !phone,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

beforeEach(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function renderPhone(locale: "zh-CN" | "en") {
  stubViewport(true);
  act(() => root.render(<TableExample locale={locale} />));
}

function byTestId(testId: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-test-id="${testId}"]`);
}

function texts(selector: string, scope: ParentNode = container): string[] {
  return [...scope.querySelectorAll<HTMLElement>(selector)].map((node) => (node.textContent ?? "").trim());
}

describe("示例表在手机上的卡片形态", () => {
  it("整张表换成卡片,表头的漏斗不再存在", () => {
    renderPhone("zh-CN");
    expect(byTestId("examples-table-cards")).not.toBeNull();
    expect(container.querySelector("table")).toBeNull();
    // 表头检索的漏斗图标是 antd 表格那一侧的东西,卡片模式下没有表头。
    expect(byTestId("q-search-icon")).toBeNull();
  });

  it("名称当标题行,其余三列排成定义表", () => {
    renderPhone("zh-CN");
    const card = container.querySelector<HTMLElement>('[data-test-id="examples-table-cards-card"]');
    expect(card).not.toBeNull();
    // 标题行是卡片里第一个块,`mobile: "title"` 指的就是名称列。
    const title = card?.querySelector<HTMLElement>("div.flex > div");
    expect(title?.textContent ?? "").toMatch(/^(Sample record|示例记录) \d{2}$/);
    // 定义表按列序排:状态 / 负责人 / 更新时间,名称不再重复出现。
    expect(texts("dt", card!)).toEqual(["状态", "负责人", "更新时间"]);
    const values = texts("dd", card!);
    expect(values).toHaveLength(3);
    expect(["在用", "暂停", "已归档"]).toContain(values[0]);
    expect(values[1].length).toBeGreaterThan(0);
    expect(values[2]).toMatch(/^2026-\d{2}-\d{2}$/);
  });

  it("表头的检索 / 筛选 / 排序搬进卡片工具条,分页留在底部", () => {
    renderPhone("zh-CN");
    expect(byTestId("examples-table-cards-toolbar")).not.toBeNull();
    expect(byTestId("examples-table-cards-search-q")).not.toBeNull();
    expect(byTestId("examples-table-cards-filter-status")).not.toBeNull();
    expect(byTestId("examples-table-cards-sort")).not.toBeNull();
    // 服务端分页:30 行 / 每页 20,分页器始终在。
    expect(byTestId("examples-table-cards-pagination")).not.toBeNull();
  });

  it("英文界面用的是英文卡片文案,不掉回中文缺省值", () => {
    renderPhone("en");
    expect(byTestId("examples-table-cards-sort")?.getAttribute("aria-label")).toBe("Sort");
    // 单选筛选的第一项是「列名:不限」。
    expect(texts('[data-test-id="examples-table-cards-filter-status"] option')[0]).toBe("Status:All");
    expect(texts("dt", container.querySelector('[data-test-id="examples-table-cards-card"]')!)).toEqual([
      "Status",
      "Owner",
      "Updated",
    ]);
  });
});
