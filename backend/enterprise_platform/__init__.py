"""可复用企业应用平台内核。

该包不得导入 EasyTrade 的 ``app`` 包。宿主通过 ports 提供账号、权限、通知、
集成配置与健康快照的持久化实现，HTTP 合同和通用安全逻辑只维护一份。
"""

from enterprise_platform.assembly import (
    PlatformPorts,
    PlatformRouteGroups,
    PlatformSecurityHooks,
    create_platform_router,
)
from enterprise_platform.authorization import create_authorization_operations_router
from enterprise_platform.oidc_settings import normalize_oidc_settings, oidc_client_authority
from enterprise_platform.ports import AuthorizationOperationsPort

__all__ = [
    "PlatformPorts",
    "PlatformRouteGroups",
    "PlatformSecurityHooks",
    "create_platform_router",
    "AuthorizationOperationsPort",
    "create_authorization_operations_router",
    "normalize_oidc_settings",
    "oidc_client_authority",
]
