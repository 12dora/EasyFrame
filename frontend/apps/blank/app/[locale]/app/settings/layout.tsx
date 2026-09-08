import { localeOf } from "../../../../lib/messages";
import { BlankSettingsFrame } from "../../../../components/blank-settings-frame";

export default async function SettingsLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  return <BlankSettingsFrame locale={localeOf(locale)}>{children}</BlankSettingsFrame>;
}
