import type { Metadata } from "next";
import { localeOf, messages } from "../../lib/messages";

export async function generateMetadata({ params }: { params: Promise<{ locale: string }> }): Promise<Metadata> {
  const { locale: rawLocale } = await params;
  return messages(localeOf(rawLocale)).metadata;
}

export default function LocaleLayout({ children }: { children: React.ReactNode }) {
  return children;
}
