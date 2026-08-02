import { localeOf, messages } from "../../../../lib/messages";
import { BlankSettingsFrame } from "../../../../components/blank-settings-frame";

export default async function SettingsLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  const labels = messages(localeOf(locale));
  return <BlankSettingsFrame locale={localeOf(locale)} title={labels.navigation.settings}>{children}</BlankSettingsFrame>;
}
