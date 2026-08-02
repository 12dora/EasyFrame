"use client";

import { SignedOutSurface } from "@easy-enterprise/ui/enterprise";
import Link from "next/link";
import { useParams } from "next/navigation";
import { localeOf, messages } from "../../../lib/messages";
import { BlankPublicShell } from "../../../components/blank-public-shell";

export default function LoggedOutPage() { const params = useParams<{ locale: string }>(); const locale = localeOf(params.locale); const t = messages(locale); return <BlankPublicShell locale={locale}><main><SignedOutSurface eyebrow={t.brand} title={t.public.loggedOutTitle} description={t.public.loggedOutDescription} actionHref={`/${locale}/login`} actionLabel={t.public.loginAgain} renderLink={({ href, className, testId, children }) => <Link href={href} className={className} data-test-id={testId}>{children}</Link>}/></main></BlankPublicShell>; }
