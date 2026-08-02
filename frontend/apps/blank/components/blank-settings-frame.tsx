"use client";

import { EnterpriseSettingsPageFrame } from "@easy-enterprise/ui/enterprise";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useBlankShellIdentity } from "./blank-shell";
import type { Locale } from "../lib/messages";

export function BlankSettingsFrame({ locale, title, children }: { locale: Locale; title: string; children: ReactNode }) {
  const pathname = usePathname();
  const identity = useBlankShellIdentity();
  const forcedTarget = `/${locale}/app/settings/security/password`;
  if (identity.mustChangePassword && pathname === forcedTarget) return children;
  return <EnterpriseSettingsPageFrame title={title}>{children}</EnterpriseSettingsPageFrame>;
}
