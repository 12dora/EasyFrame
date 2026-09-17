import { headers } from "next/headers";
import { Toaster } from "@easy-enterprise/ui";
import { localeOf } from "../../lib/messages";
import "../globals.css";

/**
 * 不带语言前缀的根布局:只服务 `/` 与 OIDC 回调页 `/login/oidc-complete`。
 *
 * 这几页没有语言段,`lang` 只能按中间件写入的请求头定,所以 `headers()` 只留在这里;
 * 业务页面都在 `[locale]` 根布局下,不受这里的动态渲染影响。
 */
export default async function BareRootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const locale = localeOf((await headers()).get("x-enterprise-locale") ?? "zh-CN");
  return <html lang={locale}><body className="min-h-dvh bg-paper text-ink">{children}<Toaster /></body></html>;
}
