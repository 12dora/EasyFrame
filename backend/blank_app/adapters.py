"""blank host 对共享 enterprise_platform ports 的 PostgreSQL 适配。

物理实现:
- ``adapter_support``: 密钥/签名/设置/审计/本地资格
- ``adapter_account``: 账号端口、本地账户管理、权限投影
- ``adapter_platform``: 页脚、通知、身份集成、上游健康
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pyotp
from fastapi.encoders import jsonable_encoder
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import and_, delete, func, or_, update
from sqlalchemy.exc import IntegrityError

from blank_app import adapter_account, adapter_platform, adapter_support
from blank_app.database import SessionLocal
from blank_app.models import (
    Account,
    HealthSnapshot,
    Notification,
    Passkey,
    PermissionCatalog,
    PermissionSnapshot,
    PlatformAuditLog,
    PlatformSetting,
)
from blank_app.permission_registry import FRAMEWORK_PERMISSIONS
from enterprise_platform import auth as shared_auth_secrets
from enterprise_platform import passkeys as shared_passkeys
from enterprise_platform.auth import AuthError
from enterprise_platform.authz import (
    CatalogPermission,
    DataScope,
    EasyAuthClientError,
    EasyAuthForbiddenError,
    EasyAuthPermissionClient,
    NormalizedGrant,
    classify_connection_failure,
    normalize_grants,
    normalize_local_grants,
)
from enterprise_platform.footer import sanitize_footer_html
from enterprise_platform.health import safe_health_summary
from enterprise_platform.jwks import probe_jwks
from enterprise_platform.local_accounts import (
    BASELINE_SELF_SERVICE,
    LocalAccountAdminPort,
    LocalAccountRecord,
    LocalPermissionRecord,
)
from enterprise_platform.oidc_settings import (
    normalize_base_url,
    normalize_oidc_settings,
    oidc_client_authority,
    rewrite_for_server_side,
)
from enterprise_platform.ports import LocalAccount, PasskeyChallenge
from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request
from enterprise_platform.schemas import (
    ConnectionTestResult,
    CurrentUser,
    DirectorySettings,
    DirectorySettingsUpdate,
    DirectorySyncResult,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    FooterSettings,
    IdentityDiscoveryResponse,
    NotificationItem,
    NotificationPage,
    OidcSettings,
    OidcSettingsUpdate,
    PasskeySummary,
    PermissionRequestUrlUpdate,
    SecurityCapabilities,
    UpstreamHealthItem,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret, encrypt_secret
from enterprise_platform.trusted_http import create_trusted_authority_transport

# 支撑层
ALL_PERMISSIONS = adapter_support.ALL_PERMISSIONS
JWT_ALGORITHM = adapter_support.JWT_ALGORITHM
MIN_SIGNING_SECRET_LENGTH = adapter_support.MIN_SIGNING_SECRET_LENGTH
OIDC_STATE_KEY_PURPOSE = adapter_support.OIDC_STATE_KEY_PURPOSE
PASSKEY_STATE_KEY_PURPOSE = adapter_support.PASSKEY_STATE_KEY_PURPOSE
PREVIOUS_SIGNING_SECRET_ENV = adapter_support.PREVIOUS_SIGNING_SECRET_ENV
PUBLIC_SECRET_MARKERS = adapter_support.PUBLIC_SECRET_MARKERS
SECURITY_OPERATION_CAPABILITIES = adapter_support.SECURITY_OPERATION_CAPABILITIES
SESSION_KEY_PURPOSE = adapter_support.SESSION_KEY_PURPOSE
SIGNING_SECRET_ENV = adapter_support.SIGNING_SECRET_ENV
TIMING_EQUALIZER_HASH = adapter_support.TIMING_EQUALIZER_HASH
_PASSKEY_CHALLENGE_KEY_PREFIX = adapter_support._PASSKEY_CHALLENGE_KEY_PREFIX
_allow_local_outbound = adapter_support._allow_local_outbound
_decrypt_saved_secret = adapter_support._decrypt_saved_secret
_get_setting = adapter_support._get_setting
_redacted_setting = adapter_support._redacted_setting
_root_signing_secrets = adapter_support._root_signing_secrets
_save_setting = adapter_support._save_setting
account_is_eligible = adapter_support.account_is_eligible
authorize_security_operation = adapter_support.authorize_security_operation
derive_signing_key = adapter_support.derive_signing_key
is_local_superadmin = adapter_support.is_local_superadmin
is_unsafe_bootstrap_secret = adapter_support.is_unsafe_bootstrap_secret
local_auth_mode = adapter_support.local_auth_mode
logger = adapter_support.logger
pwd_context = adapter_support.pwd_context
record_login_audit = adapter_support.record_login_audit
record_platform_audit = adapter_support.record_platform_audit
request_principal_account_id = adapter_support.request_principal_account_id
request_token = adapter_support.request_token
security_capabilities = adapter_support.security_capabilities
seed_default_admin = adapter_support.seed_default_admin
signing_key = adapter_support.signing_key
validate_signing_secrets = adapter_support.validate_signing_secrets
verification_keys = adapter_support.verification_keys

# 账号/本地账户
BlankAccountAdapter = adapter_account.BlankAccountAdapter
BlankLocalAccountAdmin = adapter_account.BlankLocalAccountAdmin
BlankLocalAccountUnitOfWork = adapter_account.BlankLocalAccountUnitOfWork
_account_projection = adapter_account._account_projection
_catalog_permissions = adapter_account._catalog_permissions
_consume_passkey_challenge = adapter_account._consume_passkey_challenge
_decode_session_token = adapter_account._decode_session_token
_local_grants = adapter_account._local_grants
_passkey_challenge_key = adapter_account._passkey_challenge_key
_passkey_config = adapter_account._passkey_config
_persist_passkey_challenge = adapter_account._persist_passkey_challenge
_snapshot_grants = adapter_account._snapshot_grants
_snapshot_role_groups = adapter_account._snapshot_role_groups
_superadmin_grants = adapter_account._superadmin_grants
account_adapter = adapter_account.account_adapter
local_account_admin = adapter_account.local_account_admin
require_permission = adapter_account.require_permission

# 页脚/通知/集成/上游
BlankDirectoryAdapter = adapter_platform.BlankDirectoryAdapter
BlankFooterAdapter = adapter_platform.BlankFooterAdapter
BlankIntegrationAdapter = adapter_platform.BlankIntegrationAdapter
BlankNotificationAdapter = adapter_platform.BlankNotificationAdapter
BlankUpstreamHealthAdapter = adapter_platform.BlankUpstreamHealthAdapter
_as_utc = adapter_platform._as_utc
_decode_notification_cursor = adapter_platform._decode_notification_cursor
_encode_notification_cursor = adapter_platform._encode_notification_cursor
_health_summary_code = adapter_platform._health_summary_code

__all__ = [
    "ALL_PERMISSIONS",
    "Account",
    "Any",
    "AuthError",
    "BASELINE_SELF_SERVICE",
    "BlankAccountAdapter",
    "BlankDirectoryAdapter",
    "BlankFooterAdapter",
    "BlankIntegrationAdapter",
    "BlankLocalAccountAdmin",
    "BlankLocalAccountUnitOfWork",
    "BlankNotificationAdapter",
    "BlankUpstreamHealthAdapter",
    "CatalogPermission",
    "ConnectionTestResult",
    "ContextVar",
    "CryptContext",
    "CurrentUser",
    "DataScope",
    "DirectorySettings",
    "DirectorySettingsUpdate",
    "DirectorySyncResult",
    "EasyAuthClientError",
    "EasyAuthForbiddenError",
    "EasyAuthPermissionClient",
    "EasyAuthSettingsUpdate",
    "EasyAuthStatus",
    "FRAMEWORK_PERMISSIONS",
    "FooterSettings",
    "HealthSnapshot",
    "IdentityDiscoveryResponse",
    "IntegrityError",
    "JWTError",
    "JWT_ALGORITHM",
    "LocalAccount",
    "LocalAccountAdminPort",
    "LocalAccountRecord",
    "LocalPermissionRecord",
    "MIN_SIGNING_SECRET_LENGTH",
    "NormalizedGrant",
    "Notification",
    "NotificationItem",
    "NotificationPage",
    "OIDC_STATE_KEY_PURPOSE",
    "OidcSettings",
    "OidcSettingsUpdate",
    "PASSKEY_STATE_KEY_PURPOSE",
    "PREVIOUS_SIGNING_SECRET_ENV",
    "PUBLIC_SECRET_MARKERS",
    "Passkey",
    "PasskeyChallenge",
    "PasskeySummary",
    "PermissionCatalog",
    "PermissionRequestUrlUpdate",
    "PermissionSnapshot",
    "PlatformAuditLog",
    "PlatformSetting",
    "SECURITY_OPERATION_CAPABILITIES",
    "SESSION_KEY_PURPOSE",
    "SIGNING_SECRET_ENV",
    "SecretConfigurationError",
    "SecurityCapabilities",
    "SessionLocal",
    "TIMING_EQUALIZER_HASH",
    "UTC",
    "UnsafeOutboundUrlError",
    "UpstreamHealthItem",
    "account_adapter",
    "account_is_eligible",
    "and_",
    "annotations",
    "authorize_security_operation",
    "base64",
    "classify_connection_failure",
    "create_trusted_authority_transport",
    "datetime",
    "decrypt_secret",
    "delete",
    "derive_signing_key",
    "encrypt_secret",
    "func",
    "guarded_request",
    "hashlib",
    "hmac",
    "httpx",
    "is_local_superadmin",
    "is_unsafe_bootstrap_secret",
    "jsonable_encoder",
    "jwt",
    "local_account_admin",
    "local_auth_mode",
    "logger",
    "logging",
    "normalize_base_url",
    "normalize_grants",
    "normalize_local_grants",
    "normalize_oidc_settings",
    "oidc_client_authority",
    "or_",
    "os",
    "probe_jwks",
    "pwd_context",
    "pyotp",
    "record_login_audit",
    "record_platform_audit",
    "request_principal_account_id",
    "request_token",
    "require_permission",
    "rewrite_for_server_side",
    "safe_health_summary",
    "sanitize_footer_html",
    "security_capabilities",
    "seed_default_admin",
    "shared_auth_secrets",
    "shared_passkeys",
    "signing_key",
    "timedelta",
    "update",
    "uuid",
    "validate_signing_secrets",
    "verification_keys",
]
