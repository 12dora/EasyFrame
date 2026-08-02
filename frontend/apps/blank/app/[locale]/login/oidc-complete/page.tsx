import { OidcComplete } from "../../../../components/oidc-complete";

export default async function LocalizedOidcCompletePage({ params }: { params: Promise<{ locale: string }> }) { const { locale } = await params; return <OidcComplete locale={locale}/>; }
