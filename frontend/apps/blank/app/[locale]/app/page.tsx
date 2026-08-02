import { InlineNotice, PageHeader } from "@easy-enterprise/ui";
import { localeOf, messages } from "../../../lib/messages";
export default async function Dashboard({ params }: { params: Promise<{ locale: string }> }) { const { locale: raw } = await params; const t = messages(localeOf(raw)); return <div data-test-id="blank-workbench"><PageHeader eyebrow={t.navigation.dashboard} title={t.dashboardTitle} subtitle={t.dashboardDescription}/><InlineNotice tone="success" title={t.ready} message={t.readyDetail}/></div>; }
