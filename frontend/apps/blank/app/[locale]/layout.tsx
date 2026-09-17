import type { Metadata } from "next";
import { Toaster } from "@easy-enterprise/ui";
import { localeOf, messages } from "../../lib/messages";
import "../globals.css";

export async function generateMetadata({ params }: { params: Promise<{ locale: string }> }): Promise<Metadata> {
  const { locale: rawLocale } = await params;
  return messages(localeOf(rawLocale)).metadata;
}

/**
 * 带语言前缀的根布局(`[locale]` 自己持有 `<html>`)。
 *
 * `lang` 直接取路由参数,不读 `headers()`:那一读会把整棵业务树变成请求期动态渲染,
 * 每次点侧栏都要等一次无法复用的 RSC 往返。
 */
export default async function LocaleRootLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  return <html lang={localeOf(locale)}><body className="min-h-dvh bg-paper text-ink">{children}<Toaster /></body></html>;
}
