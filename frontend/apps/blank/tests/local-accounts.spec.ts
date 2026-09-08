import { expect, test, type Page, type Request } from "@playwright/test";

const locales = ["zh-CN", "en"] as const;

const sampleAccounts = {
  data: [
    {
      id: "acc-1",
      username: "operator",
      email: "operator@example.com",
      active: true,
      isAdmin: false,
      totpEnabled: true,
      passkeyCount: 0,
      mustChangePassword: false,
      permissionCount: 2,
      expiresAt: null,
      expired: false,
      createdAt: "2026-01-01T00:00:00Z",
    },
    {
      id: "acc-admin",
      username: "framework-admin",
      email: "admin@example.com",
      active: true,
      isAdmin: true,
      totpEnabled: false,
      passkeyCount: 1,
      mustChangePassword: true,
      permissionCount: 0,
      expiresAt: null,
      expired: false,
      createdAt: "2026-01-01T00:00:00Z",
    },
    {
      id: "acc-expired",
      username: "expired-user",
      email: "expired@example.com",
      active: true,
      isAdmin: false,
      totpEnabled: false,
      passkeyCount: 0,
      mustChangePassword: false,
      permissionCount: 1,
      expiresAt: "2020-01-01T00:00:00Z",
      expired: true,
      createdAt: "2019-01-01T00:00:00Z",
    },
  ],
  meta: { total: 3 },
};

/** Shape matches GET /api/v1/local-accounts/permission-catalog (v2). */
const sampleCatalog = {
  data: [
    {
      code: "accounts.local.view",
      nameZh: "查看本地账户",
      nameEn: "View local accounts",
      groupKey: "accounts",
      riskLevel: "standard",
      supportedScopes: ["ALL"],
      grantableScopes: ["ALL"],
    },
    {
      code: "accounts.local.manage",
      nameZh: "管理本地账户",
      nameEn: "Manage local accounts",
      groupKey: "accounts",
      riskLevel: "high",
      supportedScopes: ["ALL"],
      grantableScopes: ["ALL"],
    },
    {
      code: "ops.upstream_health.view",
      nameZh: "上游健康查看",
      nameEn: "Upstream health view",
      groupKey: "ops",
      riskLevel: "standard",
      supportedScopes: ["SELF", "ALL"],
      grantableScopes: ["SELF", "ALL"],
    },
    {
      code: "auth.totp.create",
      nameZh: "创建 TOTP",
      nameEn: "Create TOTP",
      groupKey: "auth",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: ["SELF"],
    },
    {
      code: "auth.totp.advance",
      nameZh: "管理 TOTP",
      nameEn: "Advance TOTP",
      groupKey: "auth",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: ["SELF"],
    },
    {
      code: "auth.passkey.view",
      nameZh: "查看通行密钥",
      nameEn: "View passkeys",
      groupKey: "auth",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: ["SELF"],
    },
    {
      code: "auth.passkey.create",
      nameZh: "创建通行密钥",
      nameEn: "Create passkeys",
      groupKey: "auth",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: ["SELF"],
    },
    {
      code: "notification.center.view",
      nameZh: "通知中心",
      nameEn: "Notification center",
      groupKey: "notification",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: ["SELF"],
    },
    // Empty grantableScopes ⇒ not grantable to local users (greyed + note).
    {
      code: "system.internal.metric",
      nameZh: "内部指标",
      nameEn: "Internal metric",
      groupKey: "system",
      riskLevel: "standard",
      supportedScopes: ["SELF"],
      grantableScopes: [],
    },
  ],
};

const BASELINE_CODES = [
  "auth.totp.create",
  "auth.totp.advance",
  "auth.passkey.view",
  "auth.passkey.create",
  "notification.center.view",
] as const;

const fullPermissions = [
  "auth.totp.create",
  "auth.totp.advance",
  "auth.passkey.view",
  "auth.passkey.create",
  "identity.integration.view",
  "identity.integration.manage",
  "authz.integration.view",
  "authz.integration.manage",
  "ops.upstream_health.view",
  "ops.upstream_health.manage",
  "notification.center.view",
  "settings.app_setting.update",
  "accounts.local.view",
  "accounts.local.manage",
];

/** Delegated manager: local-accounts only — no authz.integration.*. */
const delegatedManagerPermissions = ["accounts.local.view", "accounts.local.manage"];

type CapturedMutation = { method: string; path: string; body: unknown };

type MockPlatformOptions = {
  permissions?: string[];
  captured?: CapturedMutation[];
  accountId?: string;
  isLocalSuperadmin?: boolean;
  forcePermissionsConflict?: boolean;
};

async function mockPlatform(page: Page, options: MockPlatformOptions | string[] = fullPermissions) {
  const opts: MockPlatformOptions = Array.isArray(options)
    ? { permissions: options, captured: [] }
    : options;
  const permissions = opts.permissions ?? fullPermissions;
  const captured = opts.captured ?? [];
  const accountId = opts.accountId ?? "acc-admin";
  const isLocalSuperadmin = opts.isLocalSuperadmin ?? true;
  let grantsVersion = 3;
  // 409 is single-shot: after the conflict the server version advances so the
  // next PUT can succeed with the reloaded expectedVersion.
  let pendingPermissionsConflict = Boolean(opts.forcePermissionsConflict);

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    if (path === "/api/v1/auth/oidc/status") return json({ enabled: false, authorizePath: "" });
    if (path === "/api/v1/auth/me") {
      return json({
        id: "u1",
        name: "Framework Admin",
        email: "admin@example.com",
        avatarUrl: null,
        hasLocalPassword: true,
        permissions,
        accountId,
        isLocalSuperadmin,
        securityCapabilities: {
          passwordChange: true,
          totpStatus: true,
          totpEnroll: true,
          totpDisable: true,
          passkeyList: true,
          passkeyRegister: true,
          passkeyDelete: true,
        },
        grants: [{ permissionCode: "accounts.local.view", dataScope: "ALL" }],
      });
    }
    if (path === "/api/v1/notifications") return json({ items: [], unreadCount: 0, nextCursor: null });
    if (path === "/api/v1/app-settings/general") {
      return json({ titleZh: "", titleEn: "", subtitleZh: "", subtitleEn: "", footerHtmlZh: "企业框架 · © {year}", footerHtmlEn: "Enterprise framework · © {year}", logoDataUrl: null });
    }
    if (path === "/api/v1/users/me/totp/status") return json({ enabled: false });
    if (path === "/api/v1/users/me/passkeys") return json([]);

    // Local-accounts catalog (delegated managers must not need authz.integration.*).
    if (path === "/api/v1/local-accounts/permission-catalog") return json(sampleCatalog);

    // Authz catalog deliberately 403 for delegated-manager coverage.
    if (path === "/api/v1/authz-integration/permission-catalog") {
      return json({ detail: "Forbidden" }, 403);
    }

    if (path === "/api/v1/local-accounts" && method === "GET") return json(sampleAccounts);

    if (path === "/api/v1/local-accounts" && method === "POST") {
      let body: unknown = {};
      try {
        body = request.postDataJSON();
      } catch {
        body = {};
      }
      captured.push({ method, path, body });
      const payload = (body ?? {}) as {
        username?: string;
        email?: string | null;
        permissions?: Array<{ code: string; scope: string }>;
      };
      return json(
        {
          id: "acc-created",
          username: payload.username ?? "created",
          email: payload.email ?? null,
          active: true,
          isAdmin: false,
          totpEnabled: false,
          passkeyCount: 0,
          mustChangePassword: true,
          permissionCount: Array.isArray(payload.permissions) ? payload.permissions.length : 0,
          expiresAt: null,
          expired: false,
          createdAt: "2026-01-02T00:00:00Z",
          uiLocale: "zh-CN",
          permissions: payload.permissions ?? [],
          baselinePermissions: [...BASELINE_CODES],
          localGrantsVersion: 0,
        },
        201,
      );
    }

    if (path.startsWith("/api/v1/local-accounts/")) {
      const segments = path.split("/").filter(Boolean);
      // /api/v1/local-accounts/{id}/permissions | password | totp
      const id = segments[3]!;
      const action = segments[4];

      if (action === "permissions" && method === "PUT") {
        let body: unknown = {};
        try {
          body = request.postDataJSON();
        } catch {
          body = {};
        }
        captured.push({ method, path, body });
        if (pendingPermissionsConflict) {
          pendingPermissionsConflict = false;
          // Concurrent writer advanced the version; reload must pick this up.
          grantsVersion += 1;
          return json({ detail: "version conflict" }, 409);
        }
        const payload = (body ?? {}) as {
          permissions?: Array<{ code: string; scope: string }>;
          expectedVersion?: number;
        };
        grantsVersion += 1;
        const summary = sampleAccounts.data.find((row) => row.id === id) ?? sampleAccounts.data[0];
        return json({
          ...summary,
          uiLocale: "zh-CN",
          permissions: payload.permissions ?? [],
          baselinePermissions: [...BASELINE_CODES],
          localGrantsVersion: grantsVersion,
        });
      }

      if (action === "password" && method === "POST") return json({ ok: true });
      if (action === "totp" && method === "DELETE") return json({ ok: true });

      if (method === "GET") {
        const summary = sampleAccounts.data.find((row) => row.id === id) ?? sampleAccounts.data[0];
        return json({
          ...summary,
          uiLocale: "zh-CN",
          permissions: [{ code: "ops.upstream_health.view", scope: "ALL" }],
          baselinePermissions: [...BASELINE_CODES],
          localGrantsVersion: grantsVersion,
        });
      }

      if (method === "DELETE") {
        captured.push({ method, path, body: null });
        // Contract: 204 No Content (empty body).
        return route.fulfill({ status: 204, body: "" });
      }

      if (method === "PATCH") {
        captured.push({ method, path, body: safeBody(request) });
        return json({ ok: true });
      }
    }

    return json({});
  });

  return { captured };
}

function safeBody(request: Request): unknown {
  try {
    return request.postDataJSON();
  } catch {
    return null;
  }
}

function isPolicyCompliant(password: string) {
  return (
    password.length >= 16 &&
    /[A-Z]/.test(password) &&
    /[a-z]/.test(password) &&
    /\d/.test(password) &&
    /[^A-Za-z0-9]/.test(password)
  );
}

/** Wait until the permission picker has settled past the loading placeholder. */
async function waitForPermissionPickerReady(page: Page) {
  await expect(
    page.locator('[data-test-id="local-accounts-permission-picker"][data-catalog-loaded="true"]'),
  ).toBeVisible();
}

/**
 * Check a grant checkbox robustly: catalog readiness + retry past hydration races
 * where Playwright's bare `.check()` can fail with "did not change its state".
 */
async function checkGrantCheckbox(page: Page, testId: string) {
  await waitForPermissionPickerReady(page);
  const grant = page.locator(`[data-test-id="${testId}"] input[type="checkbox"]`);
  await expect(grant).toBeVisible();
  await expect(grant).toBeEnabled();
  await expect(async () => {
    if (await grant.isChecked()) return;
    await grant.click();
    await expect(grant).toBeChecked({ timeout: 1000 });
  }).toPass();
}

for (const locale of locales) {
  test.describe(`local accounts (${locale})`, () => {
    test("nav entry is hidden without accounts.local.view", async ({ page }) => {
      await mockPlatform(page, {
        permissions: [
          "identity.integration.view",
          "authz.integration.view",
          "ops.upstream_health.view",
          "settings.app_setting.update",
          "auth.totp.create",
          "auth.totp.advance",
          "auth.passkey.view",
          "auth.passkey.create",
          "notification.center.view",
        ],
        isLocalSuperadmin: false,
      });
      await page.setViewportSize({ width: 1280, height: 900 });
      await page.goto(`/${locale}/app/settings/access`);
      const accountsHref = `/${locale}/app/settings/accounts`;
      await expect(page.locator(`a[href="${accountsHref}"]`)).toHaveCount(0);
      await page.goto(accountsHref);
      await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
      await expect(page.locator('[data-test-id="enterprise-local-accounts"]')).toHaveCount(0);
    });

    test("page renders table with mocked list response", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await expect(page.locator('[data-test-id="enterprise-local-accounts"]')).toBeVisible();
      await expect(page.locator('[data-test-id="local-accounts-table"]')).toBeVisible();
      await expect(page.getByText("operator", { exact: true })).toBeVisible();
      await expect(page.getByText("framework-admin", { exact: true })).toBeVisible();
      const accountsLabel = locale === "en" ? "Local accounts" : "本地账户";
      await expect(page.getByRole("heading", { name: accountsLabel })).toBeVisible();
    });

    test("expired badge renders for expired accounts", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await expect(page.locator('[data-test-id="local-accounts-expired-acc-expired"]')).toBeVisible();
      const expiredLabel = locale === "en" ? "Expired" : "已过期";
      await expect(page.locator('[data-test-id="local-accounts-expired-acc-expired"]')).toContainText(
        expiredLabel,
      );
    });

    test("create modal opens and random-password button fills a policy-compliant value", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await expect(page.locator('[data-test-id="local-accounts-create-modal"]')).toBeVisible();
      await page.locator('[data-test-id="local-accounts-create-generate-password"]').click();
      const passwordInput = page.locator('[data-test-id="local-accounts-create-password-input"]');
      const password = await passwordInput.evaluate((node) => {
        if (node instanceof HTMLInputElement) return node.value;
        const nested = node.querySelector("input");
        return nested?.value ?? "";
      });
      expect(isPolicyCompliant(password)).toBe(true);
    });

    test("permission picker renders groups from local-accounts catalog", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await waitForPermissionPickerReady(page);
      await expect(page.locator('[data-test-id="local-accounts-perm-group-accounts"]')).toBeVisible();
      await expect(page.locator('[data-test-id="local-accounts-perm-group-ops"]')).toBeVisible();
      await expect(page.getByText("accounts.local.view", { exact: false }).first()).toBeVisible();
      await expect(page.getByText("ops.upstream_health.view", { exact: false }).first()).toBeVisible();
    });

    test("scope select appears only for multi-scope codes", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await waitForPermissionPickerReady(page);

      // Multi-scope: ops.upstream_health.view has SELF+ALL → scope select after check.
      await checkGrantCheckbox(page, "local-accounts-perm-ops.upstream_health.view");
      await expect(page.locator('[data-test-id="local-accounts-perm-scope-ops.upstream_health.view"]')).toBeVisible();

      // Single-scope: accounts.local.view has only ALL → no scope control.
      await checkGrantCheckbox(page, "local-accounts-perm-accounts.local.view");
      await expect(page.locator('[data-test-id="local-accounts-perm-scope-accounts.local.view"]')).toHaveCount(0);
    });

    test("non-superadmin cannot see isAdmin switch/expiry and high codes disabled", async ({ page }) => {
      await mockPlatform(page, {
        permissions: delegatedManagerPermissions,
        isLocalSuperadmin: false,
        accountId: "acc-delegate",
      });
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await waitForPermissionPickerReady(page);

      await expect(page.locator('[data-test-id="local-accounts-create-is-admin"]')).toHaveCount(0);
      await expect(page.locator('[data-test-id="local-accounts-create-expires-at"]')).toHaveCount(0);

      const high = page.locator('[data-test-id="local-accounts-perm-accounts.local.manage"] input[type="checkbox"]');
      await expect(high).toBeVisible();
      await expect(high).toBeDisabled();
      await expect(page.locator('[data-test-id="local-accounts-perm-high-accounts.local.manage"]')).toBeVisible();
    });

    test("self-row action lockout disables dangerous controls", async ({ page }) => {
      await mockPlatform(page, {
        permissions: fullPermissions,
        isLocalSuperadmin: true,
        accountId: "acc-1",
      });
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-open-acc-1"]').click();
      await expect(page.locator('[data-test-id="local-accounts-edit-drawer"]')).toBeVisible();

      await expect(page.locator('[data-test-id="local-accounts-toggle-active"]')).toBeDisabled();
      await expect(page.locator('[data-test-id="local-accounts-delete"]')).toBeDisabled();
      await expect(page.locator('[data-test-id="local-accounts-reset-password-submit"]')).toBeDisabled();
      await expect(page.locator('[data-test-id="local-accounts-disable-totp"]')).toBeDisabled();
    });

    test("409 conflict path shows the conflict message, reloads detail, and retries with fresh expectedVersion", async ({
      page,
    }) => {
      const captured: CapturedMutation[] = [];
      await mockPlatform(page, {
        permissions: fullPermissions,
        isLocalSuperadmin: true,
        captured,
        forcePermissionsConflict: true,
      });
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-open-acc-1"]').click();
      await expect(page.locator('[data-test-id="local-accounts-edit-drawer"]')).toBeVisible();
      await waitForPermissionPickerReady(page);

      await checkGrantCheckbox(page, "local-accounts-perm-accounts.local.view");
      await page.locator('[data-test-id="local-accounts-save-permissions"]').click();

      await expect(page.locator('[data-test-id="local-accounts-grants-conflict"]')).toBeVisible();
      const conflictLabel =
        locale === "en"
          ? "Grants were modified by someone else"
          : "授权已被他人修改";
      await expect(page.locator('[data-test-id="local-accounts-grants-conflict"]')).toContainText(conflictLabel);

      // First PUT used the original version; conflict advanced the server to 4
      // and loadDetail reloaded it. Next save must carry expectedVersion: 4.
      await expect
        .poll(() =>
          captured.filter(
            (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
          ).length,
        )
        .toBe(1);
      const firstPut = captured.find(
        (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
      );
      expect((firstPut!.body as { expectedVersion?: number }).expectedVersion).toBe(3);

      await page.locator('[data-test-id="local-accounts-save-permissions"]').click();
      await expect
        .poll(() =>
          captured.filter(
            (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
          ).length,
        )
        .toBe(2);
      const puts = captured.filter(
        (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
      );
      expect((puts[1]!.body as { expectedVersion?: number }).expectedVersion).toBe(4);
    });

    test("zero-grantableScopes catalog row is disabled with not-grantable note", async ({ page }) => {
      await mockPlatform(page);
      await page.goto(`/${locale}/app/settings/accounts`);
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await waitForPermissionPickerReady(page);

      const checkbox = page.locator(
        '[data-test-id="local-accounts-perm-system.internal.metric"] input[type="checkbox"]',
      );
      await expect(checkbox).toBeVisible();
      await expect(checkbox).toBeDisabled();
      await expect(
        page.locator('[data-test-id="local-accounts-perm-not-grantable-system.internal.metric"]'),
      ).toBeVisible();
      const notGrantableLabel = locale === "en" ? "Not grantable to local users" : "不可授予本地用户";
      await expect(
        page.locator('[data-test-id="local-accounts-perm-not-grantable-system.internal.metric"]'),
      ).toContainText(notGrantableLabel);
    });

    test("delegated manager with only accounts.local.* loads page and catalog", async ({ page }) => {
      await mockPlatform(page, {
        permissions: delegatedManagerPermissions,
        isLocalSuperadmin: false,
        accountId: "acc-delegate",
      });
      await page.goto(`/${locale}/app/settings/accounts`);
      await expect(page.locator('[data-test-id="enterprise-local-accounts"]')).toBeVisible();
      await expect(page.locator('[data-test-id="local-accounts-table"]')).toBeVisible();
      await expect(page.getByText("operator", { exact: true })).toBeVisible();

      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await waitForPermissionPickerReady(page);
      await expect(page.locator('[data-test-id="local-accounts-catalog-error"]')).toHaveCount(0);
      await expect(page.locator('[data-test-id="local-accounts-perm-group-accounts"]')).toBeVisible();
      await expect(page.locator('[data-test-id="local-accounts-perm-group-ops"]')).toBeVisible();
      await expect(page.locator('[data-test-id="local-accounts-perm-group-auth"]')).toBeVisible();
      await expect(page.getByText("accounts.local.view", { exact: false }).first()).toBeVisible();
      await expect(page.getByText("ops.upstream_health.view", { exact: false }).first()).toBeVisible();

      // Baseline codes appear locked (checked + disabled), not as free grants.
      for (const code of BASELINE_CODES) {
        const baseline = page.locator(`[data-test-id="local-accounts-perm-baseline-${code}"] input[type="checkbox"]`);
        await expect(baseline).toBeVisible();
        await expect(baseline).toBeChecked();
        await expect(baseline).toBeDisabled();
      }
    });

    test("create POST and permission PUT exclude baseline codes and use LocalGrant shape", async ({ page }) => {
      const captured: CapturedMutation[] = [];
      await mockPlatform(page, {
        permissions: fullPermissions,
        isLocalSuperadmin: true,
        captured,
      });
      await page.goto(`/${locale}/app/settings/accounts`);

      // ── Create ──────────────────────────────────────────────────────────
      await page.locator('[data-test-id="local-accounts-create-btn"]').click();
      await expect(page.locator('[data-test-id="local-accounts-create-modal"]')).toBeVisible();
      await page.locator('[data-test-id="local-accounts-create-username"]').fill("delegate-user");
      await page.locator('[data-test-id="local-accounts-create-generate-password"]').click();

      // Select a non-baseline grant; baseline items stay locked/checked in the UI.
      await checkGrantCheckbox(page, "local-accounts-perm-ops.upstream_health.view");

      await page.locator('[data-test-id="local-accounts-create-submit"]').click();
      // Receipt modal after successful create.
      await expect(page.locator('[data-test-id="local-accounts-password-receipt"]')).toBeVisible();
      await page.locator('[data-test-id="local-accounts-password-receipt-confirm"]').click();

      await expect
        .poll(() => captured.some((item) => item.method === "POST" && item.path === "/api/v1/local-accounts"))
        .toBe(true);

      const createReq = captured.find((item) => item.method === "POST" && item.path === "/api/v1/local-accounts");
      expect(createReq).toBeTruthy();
      const createBody = createReq!.body as { permissions?: Array<{ code: string; scope: string }> };
      expect(Array.isArray(createBody.permissions)).toBe(true);
      expect(createBody.permissions?.some((g) => g.code === "ops.upstream_health.view")).toBe(true);
      for (const code of BASELINE_CODES) {
        expect(createBody.permissions?.some((g) => g.code === code)).toBe(false);
      }

      // ── Edit + save permissions ─────────────────────────────────────────
      await page.locator('[data-test-id="local-accounts-open-acc-1"]').click();
      await expect(page.locator('[data-test-id="local-accounts-edit-drawer"]')).toBeVisible();
      await waitForPermissionPickerReady(page);

      // Baseline still locked on edit.
      const baselineEdit = page.locator(
        '[data-test-id="local-accounts-perm-baseline-auth.totp.create"] input[type="checkbox"]',
      );
      await expect(baselineEdit).toBeChecked();
      await expect(baselineEdit).toBeDisabled();

      // Toggle an extra non-baseline grant, then save (footer action is always visible).
      await checkGrantCheckbox(page, "local-accounts-perm-accounts.local.manage");

      await page.locator('[data-test-id="local-accounts-save-permissions"]').click();
      await expect
        .poll(() =>
          captured.some(
            (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
          ),
        )
        .toBe(true);

      const putReq = captured.find(
        (item) => item.method === "PUT" && item.path === "/api/v1/local-accounts/acc-1/permissions",
      );
      expect(putReq).toBeTruthy();
      const putBody = putReq!.body as {
        permissions?: Array<{ code: string; scope: string }>;
        expectedVersion?: number;
      };
      expect(Array.isArray(putBody.permissions)).toBe(true);
      expect(typeof putBody.expectedVersion).toBe("number");
      // Detail mock starts with ops.upstream_health.view already granted.
      expect(putBody.permissions?.some((g) => g.code === "ops.upstream_health.view")).toBe(true);
      expect(putBody.permissions?.some((g) => g.code === "accounts.local.manage")).toBe(true);
      for (const code of BASELINE_CODES) {
        expect(putBody.permissions?.some((g) => g.code === code)).toBe(false);
      }
    });
  });
}
