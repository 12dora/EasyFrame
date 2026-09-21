"""blank host 权限注册表。"""

from enterprise_platform.assembly.contracts import NOTIFICATION_CENTER_VIEW, NOTIFICATION_SETTINGS_MANAGE
from enterprise_platform.authz import DataScope, PermissionRegistration

FRAMEWORK_PERMISSIONS = (
    PermissionRegistration(
        code="auth.totp.create",
        domain="auth",
        resource="totp",
        supported_scopes=[DataScope.SELF],
        risk_level="high",
    ),
    PermissionRegistration(
        code="auth.totp.advance",
        domain="auth",
        resource="totp",
        supported_scopes=[DataScope.SELF],
        risk_level="high",
    ),
    PermissionRegistration(
        code="auth.passkey.view",
        domain="auth",
        resource="passkey",
        supported_scopes=[DataScope.SELF],
        risk_level="standard",
    ),
    PermissionRegistration(
        code="auth.passkey.create",
        domain="auth",
        resource="passkey",
        supported_scopes=[DataScope.SELF],
        risk_level="high",
    ),
    PermissionRegistration(
        code=NOTIFICATION_CENTER_VIEW,
        domain="notification",
        resource="notification.center",
        supported_scopes=[DataScope.SELF],
        risk_level="standard",
    ),
    PermissionRegistration(
        code=NOTIFICATION_SETTINGS_MANAGE,
        domain="notification",
        resource="notification.settings",
        supported_scopes=[DataScope.ALL],
        risk_level="high",
    ),
    *(
        PermissionRegistration(
            code=code,
            domain=code.split(".", 1)[0],
            resource=code.rsplit(".", 1)[0],
            supported_scopes=[DataScope.ALL],
            risk_level="high" if code.endswith("manage") or code.endswith("update") else "standard",
        )
        for code in (
            "accounts.local.view",
            "accounts.local.manage",
            "settings.app_setting.update",
            "identity.integration.view",
            "identity.integration.manage",
            "authz.integration.view",
            "authz.integration.manage",
            "ops.upstream_health.view",
            "ops.upstream_health.manage",
        )
    ),
)
