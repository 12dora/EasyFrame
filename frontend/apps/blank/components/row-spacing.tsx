"use client";

import { RowSpacingProvider, type RowSpacing } from "@easy-enterprise/ui";
import { toast } from "@easy-enterprise/ui/toast";
import { usePathname } from "next/navigation";
import { useCallback, useState, type ReactNode } from "react";
import { writeCachedIdentity } from "../lib/identity-cache";
import { localeOf, messages } from "../lib/messages";
import { saveRowSpacing, type ShellIdentity } from "../lib/shell-adapter";

const TOAST_ID = "blank-row-spacing";

/**
 * 行距的宿主接线 —— 表格行高(`table-density.tsx`)的孪生兄弟,两份偏好各走各的。
 *
 * 档位是**账号偏好**,不是浏览器里的一份本地设置:值从 `/auth/session` 的
 * `preferences.rowSpacing` 随身份一起到,改档走 `PATCH /auth/preferences`(只送这一个键,
 * 另一份偏好不受影响)。换台机器、换个浏览器,表单的松紧还是自己习惯的那一档。
 *
 * 写回是乐观的:点下去整站立刻换档(等一趟往返再变会让人以为没点上),失败回滚到
 * 身份里的那个值并提示 —— 提示语与行高那一份共用 `appearanceSettings.saveFailed`,
 * 外观页保存失败就是这一句。成功后把新档位写回身份快照 —— 不然下一次身份复查落地时,
 * 还没刷新过的那份旧偏好会把刚改的档位顶回去。
 */
export function BlankRowSpacingProvider({ identity, children }: { identity: ShellIdentity; children: ReactNode }) {
  // 语言从地址里读(应用内每条路径都以语言段开头):这一层拿不到外壳的 `locale` prop,
  // 而写回失败时要说一句跟站点语言一致的话。
  const locale = localeOf(usePathname()?.split("/")[1] ?? "");
  const t = messages(locale);
  // 在途 / 已写回的乐观值;null 表示「以身份里的为准」。
  const [pending, setPending] = useState<RowSpacing | null>(null);
  const [saving, setSaving] = useState(false);
  const spacing = pending ?? identity.rowSpacing;

  const change = useCallback(
    async (next: RowSpacing) => {
      setPending(next);
      setSaving(true);
      try {
        const session = await saveRowSpacing(next);
        setPending(session.rowSpacing);
        writeCachedIdentity(locale, { ...identity, rowSpacing: session.rowSpacing });
      } catch {
        setPending(null);
        toast.error(t.appearanceSettings.saveFailed, { id: TOAST_ID });
      } finally {
        setSaving(false);
      }
    },
    [identity, locale, t.appearanceSettings.saveFailed],
  );

  return (
    <RowSpacingProvider value={spacing} saving={saving} onChange={change}>
      {children}
    </RowSpacingProvider>
  );
}
