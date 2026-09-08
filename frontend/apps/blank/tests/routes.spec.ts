import { expect, test, type Page } from "@playwright/test";

const locales = ["zh-CN", "en"];

async function mockPlatform(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/v1/auth/oidc/status") return json({ enabled: false, authorizePath: "" });
    if (path === "/api/v1/auth/me") return json({ id: "u1", name: "Framework Admin", email: "admin@example.com", avatarUrl: null, hasLocalPassword: true, permissions: ["auth.totp.create", "auth.totp.advance", "auth.passkey.view", "auth.passkey.create", "identity.integration.view", "identity.integration.manage", "authz.integration.view", "authz.integration.manage", "ops.upstream_health.view", "ops.upstream_health.manage", "notification.center.view", "settings.app_setting.update"], securityCapabilities: { passwordChange: true, totpStatus: true, totpEnroll: true, totpDisable: true, passkeyList: true, passkeyRegister: true, passkeyDelete: true }, grants: [{ permissionCode: "authz.integration.view", dataScope: "ALL" }] });
    if (path === "/api/v1/notifications") return json({ items: [], unreadCount: 0, nextCursor: null });
    if (path === "/api/v1/app-settings/general") return json({ titleZh: "", titleEn: "", subtitleZh: "", subtitleEn: "", footerHtmlZh: "企业框架 · © {year}", footerHtmlEn: "Enterprise framework · © {year}", logoDataUrl: null });
    if (path === "/api/v1/users/me/totp/status") return json({ enabled: false });
    if (path === "/api/v1/users/me/passkeys") return json([]);
    if (path === "/api/v1/identity-integration/settings") return json({ enabled: false, issuer: "", authorizationEndpoint: "", tokenEndpoint: "", jwksUri: "", userinfoEndpoint: "", clientId: "", hasClientSecret: false, scopes: "openid profile email", redirectBaseUrl: "", redirectUri: "", frontendBaseUrl: "", serverBaseUrl: "", authentikApiBaseUrl: "", hasAuthentikApiToken: false, userSyncEnabled: false, userSyncIntervalMinutes: 30 });
    if (path === "/api/v1/authz-integration/settings") return json({ configured: false, baseUrl: "", appKey: "enterprise-blank", authMode: "static_app_token", hasCredential: false, permissionRequestUrl: "" });
    if (path === "/api/v1/authz-integration/status") return json({ easyauth: { configured: false, baseUrl: "", appKey: "enterprise-blank", authMode: "static_app_token", hasCredential: false, timeoutSeconds: 5 }, principal: { mode: "disabled", headerName: "", issuer: null, audience: null }, catalog: { activeCount: 0, totalCount: 0 }, snapshots: { total: 0, expired: 0, latestFetchedAt: null } });
    if (path === "/api/v1/authz-integration/permission-catalog" || path === "/api/v1/authz-integration/snapshots") return json([]);
    if (path === "/api/v1/authz-integration/my-grants") return json([{ permission: "authz.integration.view", dataScope: "ALL", source: "easyauth" }]);
    if (path === "/api/v1/authz-integration/manifest") return json({ schema_version: 1, app: { app_key: "enterprise-blank", name: "Enterprise Blank" }, capabilities: [], permissions: [] });
    if (path === "/api/v1/authz-integration/descriptor-keys") return json([]);
    if (path === "/api/v1/ops/upstream-health") return json([
      { dependency: "authentik", displayName: "后端中文名称", status: "healthy", checkedAt: null, summary: "后端中文摘要", errorSummary: "raw-secret-error", summaryCode: "upstream.healthy", supported: true },
      { dependency: "authentik_directory", displayName: "后端目录", status: "unknown", checkedAt: null, summary: "后端不支持", errorSummary: "", summaryCode: "upstream.not_supported", supported: false },
    ]);
    return json({});
  });
}

for (const locale of locales) test.describe(`blank routes (${locale})`, () => {
  test.beforeEach(async ({ page }) => { await mockPlatform(page); });
  test("shared shell and framework routes are reachable", async ({ page }) => {
    await page.goto(`/${locale}/app`); await expect(page.locator('[data-test-id="blank-workbench"]')).toBeVisible(); await expect(page.locator('[data-test-id="admin-topbar-actions"]')).toBeVisible(); await expect(page.locator("main")).toHaveCount(1);
    for (const [path, marker] of [["security", "enterprise-security-settings"], ["access", "enterprise-access-settings"], ["upstream", "upstream-health-page"], ["general", "general-settings-page"]] as const) { await page.goto(`/${locale}/app/settings/${path}`); await expect(page.locator(`[data-test-id="${marker}"]`)).toBeVisible(); }
    let checkRequests = 0;
    await page.route("**/api/v1/ops/upstream-health/checks", async (route) => {
      checkRequests += 1;
      if (checkRequests === 1) await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Gateway session expired" }) });
    });
    await page.goto(`/${locale}/app/settings/upstream`);

    // A tokenless request that predates a new local login must not invalidate that new session.
    await expect(page.locator('[data-test-id="upstream-health-refresh"]')).toBeEnabled();
    const firstCheck = page.waitForRequest("**/api/v1/ops/upstream-health/checks");
    await page.locator('[data-test-id="upstream-health-refresh"]').click();
    await firstCheck;
    await page.evaluate(() => localStorage.setItem("enterprise-starter-token", "new-session-token"));
    await expect.poll(() => checkRequests).toBe(1);
    await page.waitForTimeout(350);
    await expect(page).toHaveURL(new RegExp(`/${locale}/app/settings/upstream$`));
    await expect.poll(() => page.evaluate(() => localStorage.getItem("enterprise-starter-token"))).toBe("new-session-token");

    // The same business 401 invalidates an established gateway session with no local token.
    await page.evaluate(() => localStorage.removeItem("enterprise-starter-token"));
    await expect(page.locator('[data-test-id="upstream-health-refresh"]')).toBeEnabled();
    await page.locator('[data-test-id="upstream-health-refresh"]').click();
    await expect(page).toHaveURL(new RegExp(`/${locale}/login\\?next=`));
    await expect(page.locator('[data-test-id="upstream-health-page"]')).toHaveCount(0);

    await page.goto(`/${locale}/app/notifications`);
    await expect(page.locator('[data-test-id="notification-center-page"]')).toBeVisible();
  });
  test("public login, logged-out and OIDC callback routes are reachable", async ({ page }) => {
    const response = await page.goto(`/${locale}/login`); await expect(page.locator('[data-test-id="enterprise-login-page"]')).toBeVisible(); await expect(page.locator("main")).toHaveCount(1);
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    await expect(page).toHaveTitle(locale === "en" ? "Enterprise Starter" : "企业应用框架");
    await expect(page.locator('meta[name="description"]')).toHaveAttribute("content", locale === "en" ? "Identity-ready enterprise application foundation" : "统一身份与企业应用基础设施");
    await expect(page.locator('[data-test-id="public-top-nav"]')).toBeVisible();
    expect(response?.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");
    expect(response?.headers()["x-frame-options"]).toBe("DENY");
    await page.goto(`/${locale}/logged-out`); await expect(page.locator('[data-test-id="logged-out-page"]')).toBeVisible();
    await page.goto(`/${locale}/login/oidc-complete`); await expect(page.locator('[data-test-id="oidc-complete-page"]')).toBeVisible(); await expect(page.locator('[data-test-id="oidc-complete-back-to-login"]')).toBeVisible(); await expect(page.locator("main")).toHaveCount(1);
  });
  test("OIDC callback errors are localized without reflecting untrusted query content", async ({ page }) => {
    const copy = locale === "zh-CN" ? {
      title: "上游登录失败",
      known: "上游身份提供方拒绝了本次登录（已取消或被策略拦截）。",
      unknown: "工作账号登录失败，请重试或联系管理员。",
    } : {
      title: "Upstream sign-in failed",
      known: "The upstream identity provider denied the sign-in (cancelled or blocked by policy).",
      unknown: "Work-account sign-in failed. Please try again or contact an administrator.",
    };
    const rawKind = "vendor_private_failure_7421"; const rawDetail = "upstream-secret-detail-9834";

    await page.goto(`/${locale}/login?oidc_error=access_denied&oidc_error_detail=${rawDetail}`);
    await expect(page.getByText(copy.title, { exact: true })).toBeVisible();
    await expect(page.getByText(copy.known, { exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(rawDetail);

    await page.goto(`/${locale}/login?oidc_error=${rawKind}&oidc_error_detail=${rawDetail}`);
    await expect(page.getByText(copy.unknown, { exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(rawKind);
    await expect(page.locator("body")).not.toContainText(rawDetail);

    await page.goto(`/${locale}/login?oidc_error_detail=${rawDetail}`);
    await expect(page.getByText(copy.title, { exact: true })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText(rawDetail);
  });
  test("OIDC status failures are localized, retryable, and distinct from disabled", async ({ page }) => {
    let attempts = 0;
    await page.route("**/api/v1/auth/oidc/status", (route) => {
      attempts += 1;
      return attempts === 1
        ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "private-upstream-message" }) })
        : route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: false, authorizePath: "" }) });
    });
    await page.goto(`/${locale}/login`);
    await expect(page.locator('[data-test-id="login-oidc-status-error"]')).toContainText(locale === "en" ? "could not be loaded" : "无法读取");
    await expect(page.locator("body")).not.toContainText("private-upstream-message");
    await page.locator('[data-test-id="login-oidc-retry"]').click();
    await expect(page.locator('[data-test-id="login-oidc-status-error"]')).toHaveCount(0);
    expect(attempts).toBe(2);
  });
  test("permission-gated blank routes return an explicit 403 surface", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "plain", name: "Plain user", hasLocalPassword: false, permissions: [] }) }));
    for (const path of ["security", "access", "upstream", "general"]) {
      await page.goto(`/${locale}/app/settings/${path}`);
      await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
      await expect(page.locator('[data-test-id="blank-auth-loading"]')).toHaveCount(0);
    }
    await page.goto(`/${locale}/app/notifications`);
    await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
  });
  test("missing security capabilities fail closed despite local password and permissions", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "legacy", name: "Legacy user", hasLocalPassword: true, permissions: ["auth.totp.advance", "auth.totp.create", "auth.passkey.view", "auth.passkey.create"] }) }));
    await page.goto(`/${locale}/app/settings/security`);
    await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
    await expect(page.locator('[data-test-id="topbar-user-menu-security"]')).toHaveCount(0);
  });
  test("register and delete passkey capabilities are enforced independently", async ({ page }) => {
    let capabilities = { passwordChange: false, totpStatus: false, totpEnroll: false, totpDisable: false, passkeyList: true, passkeyRegister: true, passkeyDelete: false };
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "passkey", name: "Passkey user", hasLocalPassword: false, permissions: ["auth.passkey.view", "auth.passkey.create"], securityCapabilities: capabilities }) }));
    await page.route("**/api/v1/users/me/passkeys", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ id: "key-1", name: "Laptop", createdAt: null, lastUsedAt: null }]) }));
    await page.goto(`/${locale}/app/settings/security`);
    await expect(page.locator('[data-test-id="passkey-add-btn"]')).toBeVisible();
    await expect(page.locator('[data-test-id="passkey-delete-btn-key-1"]')).toHaveCount(0);
    capabilities = { ...capabilities, passkeyRegister: false, passkeyDelete: true };
    await page.reload();
    await expect(page.locator('[data-test-id="passkey-add-btn"]')).toHaveCount(0);
    await expect(page.locator('[data-test-id="passkey-delete-btn-key-1"]')).toBeVisible();
  });
  test("normal password change clears local session and returns to localized login success", async ({ page }) => {
    await page.addInitScript(() => { localStorage.setItem("enterprise-starter-token", "stale-token"); localStorage.setItem("enterprise-starter-auth-method", "local"); });
    await page.goto(`/${locale}/app/settings/security`);
    await page.locator('[data-test-id="change-password-current"]').fill("old-password");
    await page.locator('[data-test-id="change-password-new"]').fill("new-password-123");
    await page.locator('[data-test-id="change-password-confirm"]').fill("new-password-123");
    await page.locator('[data-test-id="change-password-submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/${locale}/login\\?password_changed=1$`));
    await expect(page.getByText(locale === "en" ? "Password changed" : "密码已修改", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => [localStorage.getItem("enterprise-starter-token"), localStorage.getItem("enterprise-starter-auth-method")])).toEqual([null, null]);
  });
  test("local password self-service, locale state, and full logout remain reachable", async ({ page }) => {
    await page.goto(`/${locale}/app?source=review#security`);
    await page.locator('[data-test-id="topbar-user-trigger"]').click();
    await expect(page.locator('[data-test-id="topbar-user-menu-security"]')).toHaveAttribute("href", `/${locale}/app/settings/security`);
    await page.locator('[data-test-id="topbar-language-switcher"] button').click();
    const nextLocale = locale === "en" ? "zh-CN" : "en";
    await page.locator(`[data-test-id="topbar-language-option-${nextLocale}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/${nextLocale}/app\\?source=review#security$`));

    await page.locator('[data-test-id="topbar-user-trigger"]').click();
    const logoutRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/auth/logout" && request.method() === "POST");
    await page.locator('[data-test-id="topbar-user-menu-logout"]').click();
    await logoutRequest;
    await expect(page).toHaveURL(new RegExp(`/${nextLocale}/logged-out$`));
    await expect(page.locator('[data-test-id="logged-out-page"]')).toBeVisible();
  });
  test("view-only EasyAuth users never request manage-only manifest or descriptor APIs", async ({ page }) => {
    let manageOnlyReads = 0;
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "viewer", name: "Viewer", hasLocalPassword: false, permissions: ["authz.integration.view"], grants: [{ permissionCode: "authz.integration.view", dataScope: "ALL" }] }) }));
    for (const path of ["manifest", "descriptor-keys"]) await page.route(`**/api/v1/authz-integration/${path}**`, (route) => { manageOnlyReads += 1; return route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ detail: "Forbidden" }) }); });
    await page.goto(`/${locale}/app/settings/access`);
    await expect(page.locator('[data-test-id="authz-integration-status-card"]')).toBeVisible();
    // restrictedViewer(无 manage 权限)只看到状态卡:目录/我的授权/密钥全部收敛,
    // 这是审计后的收敛面裁决;本用例的核心断言是绝不发 manage-only 请求。
    await expect(page.locator('[data-test-id="authz-permission-catalog"]')).toHaveCount(0);
    expect(manageOnlyReads).toBe(0);
  });
  test("settings drill-down can return and nested routes keep the active leaf", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`/${locale}/app/settings/security/password`);
    const securityHref = `/${locale}/app/settings/security`;
    await expect(page.locator(`a[href="${securityHref}"]`).first()).toHaveAttribute("aria-current", "page");
    await expect(page.locator('[data-test-id="change-password-back"]')).toHaveAttribute("href", securityHref);
    const backLabel = locale === "en" ? "Back to main menu" : "返回主菜单";
    await page.getByRole("button", { name: backLabel }).click();
    const settingsLabel = locale === "en" ? "Settings" : "设置";
    await expect(page.getByRole("button", { name: settingsLabel })).toBeVisible();
    await page.waitForTimeout(100);
    await expect(page.getByRole("button", { name: settingsLabel })).toBeVisible();
  });
  test("mobile title uses the active settings leaf", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/${locale}/app/settings/security/password`);
    await expect(page.locator('[data-test-id="admin-mobile-nav-current"]')).toHaveText(locale === "en" ? "Security" : "安全");
  });
  test("access and upstream copy stays complete in the selected locale", async ({ page }) => {
    await page.goto(`/${locale}/app/settings/access`);
    const loginCopy = locale === "en" ? ["Login & Permissions", "Authentik / OIDC configuration"] : ["登录与权限", "Authentik / OIDC 配置"];
    for (const text of loginCopy) await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
    await page.locator('[data-test-id="settings-auth-tab-permissions"]').click();
    const permissionCopy = locale === "en" ? ["EasyAuth authorization", "My grants", "Descriptor keys"] : ["EasyAuth 授权", "我的授权", "Descriptor 密钥"];
    for (const text of permissionCopy) await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText(locale === "en" ? "我的授权" : "My grants");
    await page.goto(`/${locale}/app/settings/upstream`);
    await expect(page.getByText(locale === "en" ? "Authentik (SSO)" : "Authentik（SSO 登录）", { exact: true })).toBeVisible();
    await expect(page.getByText(locale === "en" ? "Connection healthy" : "连接正常", { exact: true })).toBeVisible();
    await expect(page.getByText(locale === "en" ? "This host does not provide this capability" : "当前宿主不提供此能力", { exact: true }).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("raw-secret-error");
    await expect(page.locator("body")).not.toContainText("后端中文摘要");
  });
  test("must-change-password is enforced on direct and refreshed protected routes", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "forced", name: "Forced user", hasLocalPassword: true, mustChangePassword: true, permissions: [], securityCapabilities: { passwordChange: true, totpStatus: false, totpEnroll: false, totpDisable: false, passkeyList: false, passkeyRegister: false, passkeyDelete: false } }) }));
    await page.goto(`/${locale}/app`);
    await expect(page).toHaveURL(new RegExp(`/${locale}/app/settings/security/password$`));
    await expect(page.locator('[data-test-id="blank-change-password-page"]')).toBeVisible();
    await expect(page.locator('[data-test-id="change-password-forced-notice"]')).toContainText(locale === "en" ? "must change" : "必须修改");
    await expect(page.locator("aside")).toHaveCount(0);
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`/${locale}/app/settings/security/password$`));
    await page.evaluate(() => { localStorage.setItem("enterprise-starter-token", "forced-token"); localStorage.setItem("enterprise-starter-auth-method", "local"); });
    await page.locator('[data-test-id="change-password-current"]').fill("old-password");
    await page.locator('[data-test-id="change-password-new"]').fill("new-password-123");
    await page.locator('[data-test-id="change-password-confirm"]').fill("new-password-123");
    await page.locator('[data-test-id="change-password-submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/${locale}/login\\?password_changed=1$`));
    await expect(page.getByText(locale === "en" ? "Password changed" : "密码已修改", { exact: true })).toBeVisible();
  });
});
