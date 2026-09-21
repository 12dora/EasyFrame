"""blank host 通知场景目录(声明 only,不接线发送)。"""

from enterprise_platform.assembly.contracts import NOTIFICATION_CENTER_VIEW, SETTINGS_UPDATE
from enterprise_platform.notification_settings import (
    LocalizedText,
    NotificationCatalog,
    NotificationGroup,
    NotificationScene,
)


def _text(zh: str, en: str) -> LocalizedText:
    return LocalizedText(zh=zh, en=en)


notification_catalog = NotificationCatalog(
    groups=(
        NotificationGroup(
            key="member",
            title=_text("成员", "Member"),
            description=_text("与你的账号相关的通知", "Notifications about your account"),
            gate_permission=NOTIFICATION_CENTER_VIEW,
            scenes=(
                NotificationScene(
                    key="account.security_alert",
                    title=_text("安全提醒", "Security alert"),
                    description=_text(
                        "账号出现新的登录或安全设置变更时",
                        "When there's a new sign-in or a security setting changes",
                    ),
                ),
                NotificationScene(
                    key="account.access_changed",
                    title=_text("权限变更", "Access changed"),
                    description=_text("你的权限发生变化时", "When your access changes"),
                ),
            ),
        ),
        NotificationGroup(
            key="administrator",
            title=_text("管理员", "Administrator"),
            description=_text("与平台运行相关的通知", "Notifications about platform operations"),
            gate_permission=SETTINGS_UPDATE,
            scenes=(
                NotificationScene(
                    key="platform.sync_failed",
                    title=_text("同步失败", "Sync failed"),
                    description=_text("用户目录同步失败时", "When user directory sync fails"),
                ),
            ),
        ),
    )
)
