import { BlankShell } from "../../../components/blank-shell";
export default async function ProtectedLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) { const { locale } = await params; return <BlankShell locale={locale}>{children}</BlankShell>; }
