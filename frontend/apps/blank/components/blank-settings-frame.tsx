"use client";

import { EnterpriseSettingsPageFrame } from "@easy-enterprise/ui/enterprise";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useBlankShellIdentity } from "./blank-shell";
import type { Locale } from "../lib/messages";

/**
 * Width/centering wrapper for every settings route. It deliberately renders no
 * heading: each settings surface owns its single `PageHeader`, so the page never
 * stacks a product-level title above the leaf title.
 */
export function BlankSettingsFrame({ locale, children }: { locale: Locale; children: ReactNode }) {
  const pathname = usePathname();
  const identity = useBlankShellIdentity();
  const forcedTarget = `/${locale}/app/settings/security/password`;
  if (identity.mustChangePassword && pathname === forcedTarget) return children;
  return <EnterpriseSettingsPageFrame>{children}</EnterpriseSettingsPageFrame>;
}
