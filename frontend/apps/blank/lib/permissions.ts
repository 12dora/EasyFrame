/**
 * 空白站带门禁的导航 / 设置面板权限码,交给 `hasEnterpriseBusinessAccess`。
 *
 * 工作台本身登录即可达,不算「有可用功能」。通知中心也不算 —— 只能看通知的账号
 * 正是引导页要接住的人。
 */
export const BLANK_BUSINESS_PERMISSION_CODES = [
  "identity.integration.view",
  "authz.integration.view",
  "accounts.local.view",
  "ops.upstream_health.view",
  "settings.app_setting.update",
] as const;
