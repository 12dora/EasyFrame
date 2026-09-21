"use client";

import { TableDensityProvider } from "@easy-enterprise/ui/table";
import { toast } from "@easy-enterprise/ui/toast";
import { usePathname } from "next/navigation";
import { type ReactNode } from "react";
import { useAccountPreference } from "./use-account-preference";
import { localeOf, messages } from "../lib/messages";
import { saveTableDensity, type ShellIdentity } from "../lib/shell-adapter";

const TOAST_ID = "blank-table-density";

/**
 * 表格行高的宿主接线。
 *
 * 档位是**账号偏好**,不是浏览器里的一份本地设置:值从 `/auth/session` 的
 * `preferences.tableDensity` 随身份一起到,改档走 `PATCH /auth/preferences`。
 * 换台机器、换个浏览器,列表还是自己习惯的那一档。
 *
 * 写回是乐观的:点下去表格立刻换档(等一趟往返再变会让人以为没点上),失败回滚到
 * 身份里的那个值并提示。成功后把新档位写回身份快照 —— 不然下一次身份复查落地时,
 * 还没刷新过的那份旧偏好会把刚改的档位顶回去。
 */
export function BlankTableDensityProvider({ identity, children }: { identity: ShellIdentity; children: ReactNode }) {
  // 语言从地址里读(应用内每条路径都以语言段开头):这一层拿不到外壳的 `locale` prop,
  // 而写回失败时要说一句跟站点语言一致的话。
  const locale = localeOf(usePathname()?.split("/")[1] ?? "");
  const t = messages(locale);
  const { value: density, saving, change } = useAccountPreference(
    identity, locale, "tableDensity", saveTableDensity,
    () => toast.error(t.appearanceSettings.saveFailed, { id: TOAST_ID }),
  );

  return (
    <TableDensityProvider value={density} saving={saving} onChange={change}>
      {children}
    </TableDensityProvider>
  );
}
