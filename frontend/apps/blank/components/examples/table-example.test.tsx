import { describe, expect, it } from "vitest";
import type { MobileColumn } from "@easy-enterprise/ui/table";
import type { TableQueryState } from "../../lib/table-query";
import { messages } from "../../lib/messages";
import { exampleColumns } from "./table-example";

/**
 * 示例表是新宿主抄的那份样板,所以手机形态的两个约定要钉住:
 *   - 名称列显式标了 `mobile: "title"`(不标就取排序后的第一列,换个默认排序标题就换人了);
 *   - `labels.cards` 的四句文案两种语言都给齐(缺了就掉回 EasyUI 的中文缺省值,
 *     英文界面上会冒出中文)。
 */

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
  });
});
