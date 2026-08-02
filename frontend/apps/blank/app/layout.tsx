import { headers } from "next/headers";
import { Toaster } from "@easy-enterprise/ui";
import { localeOf } from "../lib/messages";
import "./globals.css";

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <RootDocument>{children}</RootDocument>;
}

async function RootDocument({ children }: { children: React.ReactNode }) {
  const locale = localeOf((await headers()).get("x-enterprise-locale") ?? "zh-CN");
  return <html lang={locale}><body className="min-h-dvh bg-paper text-ink">{children}<Toaster /></body></html>;
}
