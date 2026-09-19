import type { EnterpriseGeneralSettingsValue } from "@easy-enterprise/ui/enterprise";

/**
 * 服务端取一份通用设置(只在外壳段 `app/[locale]/app/layout.tsx` 里用),交给客户端首帧。
 *
 * 为什么要在 SSR 取:全局「显示页脚」关掉之后,客户端若等自己的 GET 回来才知道,外壳会先按缺省
 * (显示)把页脚画出来再收起,工作区跟着跳一下(EasyUI `docs/GENERAL-SETTINGS.md` §3「首帧」)。
 * 外壳段本来就是 `force-dynamic`,多这一次内网读取不改变缓存形态。
 *
 * 客户端只拿它作「共享读取落地之前」的回退(`BlankShell`),不灌进共享缓存:客户端那次 GET 照发、
 * 以它为准,SSR 这份只是早到一步。
 *
 * 走内网直连(与 `next.config.ts` 的 `/api` 反代同一个 `INTERNAL_BACKEND_ORIGIN`),接口是公开的,不带凭据。
 * 失败或超时返回 `null`:客户端的共享读取照常发出、失败照常报,这里只是少了首帧的交接,
 * 退回「先按显示页脚」的缺省。超时压得很短,后端慢时不拖住整页。
 */
const INTERNAL_BACKEND_ORIGIN = process.env.INTERNAL_BACKEND_ORIGIN || "http://blank-backend:8000";
const SSR_TIMEOUT_MS = 1000;

export async function fetchInitialGeneralSettings(): Promise<EnterpriseGeneralSettingsValue | null> {
  try {
    const response = await fetch(`${INTERNAL_BACKEND_ORIGIN}/api/v1/app-settings/general`, {
      cache: "no-store",
      signal: AbortSignal.timeout(SSR_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as EnterpriseGeneralSettingsValue;
  } catch {
    return null;
  }
}
