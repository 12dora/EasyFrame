import { expect, test, type Page } from "@playwright/test";
import { createNotificationSettingsMock } from "./notification-settings-mock";

const locales = ["zh-CN", "en"];

const defaultGeneral = { titleZh: "", titleEn: "", subtitleZh: "", subtitleEn: "", footerHtmlZh: "企业框架 · © {year}", footerHtmlEn: "Enterprise framework · © {year}", logoDataUrl: null, showFooter: true };

async function mockPlatform(page: Page, general: Record<string, unknown> = defaultGeneral) {
  let stored = { ...general };
  // 「设置 → 通知」的替身自己保留可变状态(补丁累积生效),每个用例一份。
  const notificationSettings = createNotificationSettingsMock();
  // 账号偏好(表格行高、行距)住在服务端:`PATCH /auth/preferences` 改的就是这一份,`/auth/session` 再读回去;
  // 两个键各改各的(下面的合并写回就是后端那条「只动送来的那一项」的规矩)。
  let preferences: Record<string, unknown> = { tableDensity: "compact", rowSpacing: "compact" };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/v1/app-settings/general") {
      if (route.request().method() === "PUT") { stored = { ...stored, ...(JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>) }; }
      return json(stored);
    }
    if (path === "/api/v1/auth/oidc/status") return json({ enabled: false, authorizePath: "" });
    if (path === "/api/v1/auth/preferences") {
      preferences = { ...preferences, ...(JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>) };
      return json({ permissionRequestUrl: "https://easyauth.example.test/request", preferences });
    }
    if (path === "/api/v1/auth/session") return json({ permissionRequestUrl: "https://easyauth.example.test/request", preferences });
    if (path === "/api/v1/auth/me") return json({ id: "u1", name: "Framework Admin", email: "admin@example.com", avatarUrl: null, hasLocalPassword: true, permissions: ["auth.totp.create", "auth.totp.advance", "auth.passkey.view", "auth.passkey.create", "identity.integration.view", "identity.integration.manage", "authz.integration.view", "authz.integration.manage", "ops.upstream_health.view", "ops.upstream_health.manage", "notification.center.view", "settings.app_setting.update"], securityCapabilities: { passwordChange: true, totpStatus: true, totpEnroll: true, totpDisable: true, passkeyList: true, passkeyRegister: true, passkeyDelete: true }, grants: [{ permissionCode: "authz.integration.view", dataScope: "ALL" }] });
    if (path === "/api/v1/notifications") return json({ items: [], unreadCount: 0, nextCursor: null });
    if (await notificationSettings.handle(path, route)) return undefined;
    if (path === "/api/v1/users/me/totp/status") return json({ enabled: false });
    if (path === "/api/v1/users/me/passkeys") return json([]);
    if (path === "/api/v1/identity-integration/settings") return json({ enabled: false, issuer: "", authorizationEndpoint: "", tokenEndpoint: "", jwksUri: "", userinfoEndpoint: "", clientId: "", hasClientSecret: false, scopes: "openid profile email", redirectBaseUrl: "", redirectUri: "", frontendBaseUrl: "", serverBaseUrl: "" });
    if (path === "/api/v1/authz-integration/settings") return json({ configured: false, baseUrl: "", appKey: "enterprise-blank", authMode: "static_app_token", hasCredential: false, hasWebhookSecret: false, permissionRequestUrl: "" });
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

    // 未配置名称/副标题/标志时，品牌槽回落到 i18n 名称 + 内置标志，且不显示副标题。
    await expect(page.locator('[data-test-id="app-brand-title"]')).toHaveText(locale === "en" ? "Enterprise Starter" : "企业应用框架");
    await expect(page.locator('[data-test-id="app-brand-logo"]')).toBeVisible();
    await expect(page.locator('[data-test-id="app-brand-subtitle"]')).toHaveCount(0);
    // 登录后的应用框架同样固定页脚。
    await expect(page.locator('[data-test-id="app-footer-html"]')).toContainText(locale === "en" ? "Enterprise framework" : "企业框架");
    await expect(page.locator('[data-test-id="topbar-user-role"]')).toHaveText(locale === "en" ? "User" : "用户");

    for (const [path, marker] of [["general", "general-settings-page"], ["appearance", "appearance-settings-page"], ["notifications", "notification-settings-surface"], ["security", "enterprise-security-settings"], ["access", "enterprise-access-settings"], ["upstream", "upstream-health-page"]] as const) { await page.goto(`/${locale}/app/settings/${path}`); await expect(page.locator(`[data-test-id="${marker}"]`)).toBeVisible(); }
    // 设置页只保留最内层标题，框架不再叠加「设置」大标题；「通用」排在设置菜单首位。
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`/${locale}/app/settings/general`);
    await expect(page.locator('[data-test-id="enterprise-settings-page"]')).toBeVisible();
    await expect(page.locator("main h1")).toHaveText(locale === "en" ? "General" : "通用");
    await expect(page.locator(`aside a[href^="/${locale}/app/settings/"]`).first()).toHaveAttribute("href", `/${locale}/app/settings/general`);
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

    // 模板自带的示例列表页(宿主接入真实列表后连同 app/[locale]/app/examples 一起删掉):
    // 表头搜索 / 筛选的漏斗在,深链的筛选条件能还原出来。
    await page.goto(`/${locale}/app/examples/table`);
    await expect(page.locator('[data-test-id="examples-table-page"]')).toBeVisible();
    await expect(page.locator('[data-test-id="examples-table"]')).toBeVisible();
    await expect(page.locator('[data-test-id="q-search-icon"]')).toBeVisible();
    await expect(page.locator('[data-test-id="status-filter-icon"]')).toBeVisible();
    await page.goto(`/${locale}/app/examples/table?status=archived`);
    await expect(page.locator('[data-test-id="status-filter-icon"]')).toHaveAttribute("data-active", "true");
  });
  // 表格行高是账号偏好:设置页改一下,写回 `PATCH /auth/preferences`,列表页当场换档,
  // 刷新之后仍是这一档(值从 `/auth/session` 来,不在浏览器里)。
  test("table density is an account preference shared by the appearance page and every table", async ({ page }) => {
    const exampleTable = page.locator('[data-test-id="examples-table"] .ant-table').first();
    // 缺省是紧凑(antd `size="small"`)。
    await page.goto(`/${locale}/app/examples/table`);
    await expect(exampleTable).toHaveClass(/ant-table-small/);

    await page.goto(`/${locale}/app/settings/appearance`);
    await expect(page.locator('[data-test-id="appearance-settings-page"]')).toBeVisible();
    await expect(page.locator("main h1")).toHaveText(locale === "en" ? "Appearance" : "外观");
    const toggle = page.locator('[data-test-id="appearance-density-toggle"]');
    await expect(toggle.locator('[data-density="compact"]')).toHaveAttribute("data-active", "true");

    const saved = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/auth/preferences" && request.method() === "PATCH");
    await toggle.locator('[data-density="comfortable"]').click();
    expect(JSON.parse((await saved).postData() ?? "{}")).toEqual({ tableDensity: "comfortable" });
    await expect(toggle.locator('[data-density="comfortable"]')).toHaveAttribute("data-active", "true");

    // 同一档位跟到列表页(antd `size="middle"`),并且刷新之后仍然在
    // —— 值是服务端存的,不在浏览器里。
    await page.goto(`/${locale}/app/examples/table`);
    await expect(exampleTable).toHaveClass(/ant-table-medium/);
    await page.goto(`/${locale}/app/settings/appearance`);
    await expect(page.locator('[data-test-id="appearance-density-toggle"] [data-density="comfortable"]')).toHaveAttribute("data-active", "true");
  });
  /**
   * 「设置 → 通知」:任何登录用户都进得去(后端按 gate 权限过滤分组),托管分组只读,
   * 开关即点即存,`loadPolicy` 只在真的切到「平台配置」时才发。
   */
  test("notification settings save on click and keep the managed group read-only", async ({ page }) => {
    const policyCalls: string[] = [];
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/v1/notification-settings/policy") policyCalls.push(request.method()); });

    await page.goto(`/${locale}/app/settings/notifications`);
    await expect(page.locator('[data-test-id="notification-settings-surface"]')).toBeVisible();
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect(page.locator("main h1")).toHaveText(locale === "en" ? "Notifications" : "通知");
    await expect(page.locator('[data-test-id="notification-group-exam"]')).toBeVisible();
    // 托管中的分组:标题行留着那枚静态标签与一枚恒为只读的开关(它是状态展示,不是操作入口)。
    await expect(page.locator('[data-test-id="notification-group-ops-tag"]')).toBeVisible();
    await expect(page.locator('[data-test-id="notification-group-ops-managed"]')).toBeDisabled();
    await expect(page.locator('[data-test-id="notification-group-exam-tag"]')).toHaveCount(0);
    // 场景不支持的渠道画破折号,不画一个永远关着的开关。
    await expect(page.locator('[data-test-id="notification-switch-exam.reminder-dingtalk"]')).toHaveCount(0);

    // 即点即存:没有保存按钮,一次改动就是一个 PATCH /preferences,页面用返回的分组对齐。
    const saved = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/notification-settings/preferences" && request.method() === "PATCH");
    const examSwitch = page.locator('[data-test-id="notification-switch-exam.result_released-dingtalk"]');
    await examSwitch.click();
    expect(JSON.parse((await saved).postData() ?? "{}")).toEqual({ group: "exam", scene: "exam.result_released", channel: "dingtalk", enabled: false });
    await expect(examSwitch).toHaveAttribute("aria-checked", "false");

    // 切页签之前一次都不读平台值;切过去才读,而且那一页里所有分组都能改。
    expect(policyCalls).toEqual([]);
    await page.locator('[data-test-id="notification-settings-tabs"] [data-notification-tab="policy"]').click();
    await expect(page.locator('[data-test-id="notification-policy-description"]')).toBeVisible();
    await expect.poll(() => policyCalls).toEqual(["GET"]);
    await expect(page.locator('[data-test-id="notification-group-ops-managed"]')).toBeEnabled();
    await expect(page.locator('[data-test-id="notification-group-ops-tag"]')).toHaveCount(0);
  });
  test("public login, logged-out and OIDC callback routes are reachable", async ({ page }) => {
    const response = await page.goto(`/${locale}/login`); await expect(page.locator('[data-test-id="enterprise-login-page"]')).toBeVisible(); await expect(page.locator("main")).toHaveCount(1);
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    await expect(page).toHaveTitle(locale === "en" ? "Enterprise Starter" : "企业应用框架");
    await expect(page.locator('meta[name="description"]')).toHaveAttribute("content", locale === "en" ? "Identity-ready enterprise application foundation" : "统一身份与企业应用基础设施");
    await expect(page.locator('[data-test-id="public-top-nav"]')).toBeVisible();
    await expect(page.locator('[data-test-id="public-brand-title"]')).toHaveText(locale === "en" ? "Enterprise Starter" : "企业应用框架");
    await expect(page.locator('[data-test-id="public-brand-logo"]')).toBeVisible();
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
  test("an account with no business access gets the full-screen onboarding page", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "plain", name: "Plain user", email: "plain@example.com", hasLocalPassword: false, permissions: [] }) }));
    await page.goto(`/${locale}/app`);
    await expect(page.locator('[data-test-id="permission-onboarding"]')).toBeVisible();
    await expect(page.locator('[data-test-id="permission-onboarding-name"]')).toHaveText("Plain user");
    await expect(page.locator('[data-test-id="permission-onboarding-request"]')).toHaveAttribute("href", "https://easyauth.example.test/request");
    await expect(page.locator('[data-test-id="permission-onboarding-recheck"]')).toBeVisible();
    await expect(page.locator('[data-test-id="permission-onboarding-logout"]')).toBeVisible();
    await expect(page.locator('[data-test-id="admin-topbar-actions"]')).toHaveCount(0);
    await expect(page.locator('[data-test-id="blank-workbench"]')).toHaveCount(0);

    await page.route("**/api/v1/auth/session", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ permissionRequestUrl: null }) }));
    await page.goto(`/${locale}/app`);
    await expect(page.locator('[data-test-id="permission-onboarding"]')).toBeVisible();
    await expect(page.locator('[data-test-id="permission-onboarding-request"]')).toHaveCount(0);
  });
  test("permission-gated blank routes return an explicit 403 surface", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "viewer", name: "Viewer", hasLocalPassword: false, permissions: ["identity.integration.view"] }) }));
    for (const path of ["security", "upstream", "general"]) {
      await page.goto(`/${locale}/app/settings/${path}`);
      await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
      await expect(page.locator('[data-test-id="blank-auth-loading"]')).toHaveCount(0);
      await expect(page.locator('[data-test-id="permission-onboarding"]')).toHaveCount(0);
    }
    // 被拒的「通用」页仍然是那一页：标题照常渲染，且全页只有一个 H1。
    await page.goto(`/${locale}/app/settings/general`);
    await expect(page.locator('[data-test-id="general-settings-page"]')).toBeVisible();
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect(page.locator("main h1")).toHaveText(locale === "en" ? "General" : "通用");
    // 「外观」只改自己的账号偏好,没有门禁:任何登录用户都进得去;全局「显示页脚」卡片只给能改通用设置的人。
    await page.goto(`/${locale}/app/settings/appearance`);
    await expect(page.locator('[data-test-id="appearance-settings-page"]')).toBeVisible();
    await expect(page.locator('[data-test-id="permission-denied"]')).toHaveCount(0);
    await expect(page.locator('[data-test-id="appearance-global-section"]')).toHaveCount(0);
    // 「通知」同样没有门禁:分组由后端按 gate 权限过滤,一个都没有时是空状态而不是 403。
    await page.goto(`/${locale}/app/settings/notifications`);
    await expect(page.locator('[data-test-id="notification-settings-surface"]')).toBeVisible();
    await expect(page.locator('[data-test-id="permission-denied"]')).toHaveCount(0);
    await page.goto(`/${locale}/app/notifications`);
    await expect(page.locator('[data-test-id="permission-denied"]')).toBeVisible();
  });
  test("missing security capabilities fail closed despite local password and permissions", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "legacy", name: "Legacy user", hasLocalPassword: true, permissions: ["identity.integration.view", "auth.totp.advance", "auth.totp.create", "auth.passkey.view", "auth.passkey.create"] }) }));
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
  // 手机上只剩一条头部栏:汉堡在顶栏里,旧的分区栏(与它的当前分区标题)整条去掉;
  // 页脚也从页面底部挪进抽屉。见 docs/SHELL_MOBILE.md。
  test("mobile keeps one header bar and moves the footer into the drawer", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/${locale}/app/settings/security/password`);
    await expect(page.locator('[data-test-id="admin-mobile-nav"]')).toHaveCount(0);
    await expect(page.locator('[data-test-id="admin-mobile-nav-current"]')).toHaveCount(0);
    // 页面底部那一份页脚仍在 DOM 里(框架照常收 footer),手机上由 `hidden md:block` 收起。
    await expect(page.locator('[data-test-id="app-footer-html"]')).toBeHidden();
    const trigger = page.locator('[data-test-id="admin-mobile-nav-trigger"]');
    await expect(trigger).toBeVisible();
    await trigger.click();
    const drawer = page.locator('[data-test-id="admin-mobile-nav-drawer"]');
    await expect(drawer).toBeVisible();
    await expect(drawer.locator('[data-test-id="app-footer-html-inline"]')).toContainText(locale === "en" ? "Enterprise framework" : "企业框架");
    await expect(drawer.locator("footer")).toHaveCount(0);
    await expect(drawer.locator('[data-test-id="blank-nav-settings"]')).toBeVisible();
  });
  test("access and upstream copy stays complete in the selected locale", async ({ page }) => {
    await page.goto(`/${locale}/app/settings/access`);
    const loginCopy = locale === "en" ? ["Login & Permissions", "Authentik / OIDC configuration"] : ["登录与权限", "Authentik / OIDC 配置"];
    for (const text of loginCopy) await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
    await page.locator('[data-test-id="settings-auth-tab-permissions"]').click();
    const permissionCopy = locale === "en" ? ["Permissions service authorization", "My grants", "Application info keys"] : ["权限服务授权", "我的授权", "应用信息密钥"];
    for (const text of permissionCopy) await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText(locale === "en" ? "我的授权" : "My grants");
    await page.goto(`/${locale}/app/settings/upstream`);
    await expect(page.getByText(locale === "en" ? "Authentik (SSO)" : "Authentik（SSO 登录）", { exact: true })).toBeVisible();
    await expect(page.getByText(locale === "en" ? "Connection healthy" : "连接正常", { exact: true })).toBeVisible();
    await expect(page.getByText(locale === "en" ? "This host does not provide this capability" : "当前宿主不提供此能力", { exact: true }).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("raw-secret-error");
    await expect(page.locator("body")).not.toContainText("后端中文摘要");
  });
  test("configured general settings drive the brand and footer in both shells", async ({ page }) => {
    const logo = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
    await mockPlatform(page, { titleZh: "捷发企业", titleEn: "Jiefa Enterprise", subtitleZh: "统一工作台", subtitleEn: "Unified workbench", footerHtmlZh: "捷发 · © {year}", footerHtmlEn: "Jiefa · © {year}", logoDataUrl: logo });
    const expected = locale === "en" ? { title: "Jiefa Enterprise", subtitle: "Unified workbench", footer: "Jiefa" } : { title: "捷发企业", subtitle: "统一工作台", footer: "捷发" };

    await page.goto(`/${locale}/app`);
    await expect(page.locator('[data-test-id="app-brand-title"]')).toHaveText(expected.title);
    await expect(page.locator('[data-test-id="app-brand-subtitle"]')).toHaveText(expected.subtitle);
    await expect(page.locator('[data-test-id="app-brand-logo"]')).toHaveAttribute("src", logo);
    await expect(page.locator('[data-test-id="app-footer-html"]')).toContainText(`${expected.footer} · © ${new Date().getFullYear()}`);

    await page.goto(`/${locale}/login`);
    await expect(page.locator('[data-test-id="public-brand-title"]')).toHaveText(expected.title);
    await expect(page.locator('[data-test-id="public-brand-subtitle"]')).toHaveText(expected.subtitle);
    await expect(page.locator('[data-test-id="app-footer-html"]')).toContainText(expected.footer);
  });
  test("saving general settings re-brands the shell without a reload", async ({ page }) => {
    await page.goto(`/${locale}/app/settings/general`);
    await expect(page.locator('[data-test-id="general-settings-page"]')).toBeVisible();
    await page.locator('[data-test-id="general-title-zh"]').fill("捷发企业");
    await page.locator('[data-test-id="general-subtitle-zh"]').fill("统一工作台");
    await page.locator('[data-test-id="general-locale-tab-en"]').click();
    await page.locator('[data-test-id="general-title-en"]').fill("Jiefa Enterprise");
    await page.locator('[data-test-id="general-subtitle-en"]').fill("Unified workbench");
    const saved = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/app-settings/general" && request.method() === "PUT");
    await page.locator('[data-test-id="app-settings-save"]').click();
    await saved;
    await expect(page.locator('[data-test-id="app-brand-title"]')).toHaveText(locale === "en" ? "Jiefa Enterprise" : "捷发企业");
    await expect(page.locator('[data-test-id="app-brand-subtitle"]')).toHaveText(locale === "en" ? "Unified workbench" : "统一工作台");
  });
  test("the global show-footer switch hides the footer and the workspace takes its height", async ({ page }) => {
    const saved: Record<string, unknown>[] = [];
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/v1/app-settings/general" && request.method() === "PUT") saved.push(request.postDataJSON() as Record<string, unknown>); });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`/${locale}/app/settings/appearance`);
    await expect(page.locator('[data-test-id="app-footer-html"]')).toBeVisible();
    await expect(page.locator('[data-test-id="appearance-global-section"]')).toBeVisible();
    await page.locator('[data-test-id="appearance-show-footer-switch"]').click();
    // 整份通用设置原样 PUT 回去,只改这一个字段;外壳当场收起页脚,不用刷新。
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0]).toMatchObject({ footerHtmlZh: "企业框架 · © {year}", showFooter: false });
    await expect(page.locator('[data-test-id="app-footer-html"]')).toHaveCount(0);
    // 页脚那截高度归工作区:`<main>` 的下沿贴到视口底。
    const box = await page.locator("main").boundingBox();
    expect(Math.abs((box?.y ?? 0) + (box?.height ?? 0) - 900)).toBeLessThanOrEqual(1);
    // 刷新后仍然不显示(读的是保存后的值)。
    await page.reload();
    await expect(page.locator('[data-test-id="appearance-settings-page"]')).toBeVisible();
    await expect(page.locator('[data-test-id="app-footer-html"]')).toHaveCount(0);
  });
  test("the identity line reports the local superadmin and an account with no grants", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "root", name: "Root", hasLocalPassword: true, isLocalSuperadmin: true, permissions: ["accounts.local.view"] }) }));
    await page.goto(`/${locale}/app`);
    await expect(page.locator('[data-test-id="topbar-user-role"]')).toHaveText(locale === "en" ? "Administrator" : "管理员");

    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "guest", name: "Guest", hasLocalPassword: true, permissions: [], securityCapabilities: { passwordChange: true, totpStatus: false, totpEnroll: false, totpDisable: false, passkeyList: false, passkeyRegister: false, passkeyDelete: false } }) }));
    await page.goto(`/${locale}/app`);
    await expect(page.locator('[data-test-id="topbar-user-role"]')).toHaveText(locale === "en" ? "Guest" : "游客");
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
