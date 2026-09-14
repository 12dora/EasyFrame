import { expect, it } from "vitest";
import { hasEnterpriseBusinessAccess } from "@easy-enterprise/ui/enterprise";

import { BLANK_BUSINESS_PERMISSION_CODES } from "./permissions";

it("does not treat notification-centre-only as business access", () => {
  expect(
    hasEnterpriseBusinessAccess({
      permissions: new Set(["notification.center.view"]),
      businessPermissionCodes: BLANK_BUSINESS_PERMISSION_CODES,
    }),
  ).toBe(false);
});

it("treats a gated settings permission as business access", () => {
  expect(
    hasEnterpriseBusinessAccess({
      permissions: new Set(["settings.app_setting.update"]),
      businessPermissionCodes: BLANK_BUSINESS_PERMISSION_CODES,
    }),
  ).toBe(true);
});
