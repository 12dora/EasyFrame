"""blank host adapter 支撑层:密钥、签名、设置、审计与本地资格。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from passlib.context import CryptContext
from sqlalchemy import or_

from blank_app.database import SessionLocal
from blank_app.models import Account, PlatformAuditLog, PlatformSetting
from blank_app.permission_registry import FRAMEWORK_PERMISSIONS
from enterprise_platform import auth as shared_auth_secrets
from enterprise_platform.auth import AuthError
from enterprise_platform.schemas import (
    CurrentUser,
    SecurityCapabilities,
)
from enterprise_platform.secrets import decrypt_secret


def _facade():
    import blank_app.adapters as adapters

    return adapters


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
TIMING_EQUALIZER_HASH = pwd_context.hash("enterprise-platform-timing-equalizer")
logger = logging.getLogger("blank_app.adapters")
JWT_ALGORITHM = "HS256"
SIGNING_SECRET_ENV = "BLANK_JWT_SECRET"
PREVIOUS_SIGNING_SECRET_ENV = "BLANK_JWT_SECRET_PREVIOUS"
MIN_SIGNING_SECRET_LENGTH = 32
# 每个签名用途派生独立密钥:session token 与 OIDC state / Passkey 挑战互不通用。
SESSION_KEY_PURPOSE = "blank-session-v1"
OIDC_STATE_KEY_PURPOSE = "blank-oidc-state-v1"
PASSKEY_STATE_KEY_PURPOSE = "blank-passkey-state-v1"
# BE-01: blank 无独立 ceremony 表，复用有主键的 platform_settings 存 durable
# challenge rows。key 是 JTI hash；complete 在凭据事务中对 JSON consumed_at 做 CAS。
_PASSKEY_CHALLENGE_KEY_PREFIX = "passkey:"
request_token: ContextVar[str | None] = ContextVar("blank_request_token", default=None)
request_principal_account_id: ContextVar[str | None] = ContextVar("blank_principal_account_id", default=None)

ALL_PERMISSIONS = {permission.code for permission in FRAMEWORK_PERMISSIONS}

# 共享 router 传入的 operation -> SecurityCapabilities 字段;缺失即视为未知操作并拒绝。
SECURITY_OPERATION_CAPABILITIES: dict[str, str] = {
    "change_password": "password_change",
    "totp_status": "totp_status",
    "totp_begin": "totp_enroll",
    "totp_confirm": "totp_enroll",
    "totp_disable": "totp_disable",
    "passkey_list": "passkey_list",
    "passkey_register_begin": "passkey_register",
    "passkey_register_complete": "passkey_register",
    "passkey_delete": "passkey_delete",
}

# 唯一实现在 enterprise_platform.auth;此处仅保留兼容再导出。
PUBLIC_SECRET_MARKERS = shared_auth_secrets.PUBLIC_SECRET_MARKERS
is_unsafe_bootstrap_secret = shared_auth_secrets.is_unsafe_bootstrap_secret


def _root_signing_secrets() -> list[str]:
    """当前签名根密钥 + 轮换期历史根密钥(历史根密钥只用于验签)。"""

    current = os.getenv(SIGNING_SECRET_ENV, "")
    previous = [item.strip() for item in os.getenv(PREVIOUS_SIGNING_SECRET_ENV, "").split(",") if item.strip()]
    return [current, *previous]


def derive_signing_key(root_secret: str, purpose: str) -> str:
    """按用途派生签名密钥;用途不同则密钥不同,跨用途重放无法通过验签。"""

    return hmac.new(root_secret.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def signing_key(purpose: str) -> str:
    """签发用密钥:只由当前根密钥派生。"""

    return derive_signing_key(_root_signing_secrets()[0], purpose)


def verification_keys(purpose: str) -> list[str]:
    """验签用密钥:当前根密钥 + 轮换期历史根密钥,支持不中断会话的密钥轮换。"""

    return [derive_signing_key(root, purpose) for root in _root_signing_secrets() if root]


def validate_signing_secrets() -> None:
    """签名密钥门禁:与本地认证是否启用无关,启动即执行(OIDC-only 部署同样受约束)。"""

    roots = _root_signing_secrets()
    if is_unsafe_bootstrap_secret(roots[0], min_length=MIN_SIGNING_SECRET_LENGTH):
        raise RuntimeError(
            f"{SIGNING_SECRET_ENV} must be at least {MIN_SIGNING_SECRET_LENGTH} bytes and must not use a public example"
        )
    for previous in roots[1:]:
        if is_unsafe_bootstrap_secret(previous, min_length=MIN_SIGNING_SECRET_LENGTH):
            raise RuntimeError(
                f"{PREVIOUS_SIGNING_SECRET_ENV} entries must be at least {MIN_SIGNING_SECRET_LENGTH} bytes "
                "and must not use a public example"
            )


def seed_default_admin() -> None:
    mode = local_auth_mode()
    runtime = os.getenv("BLANK_RUNTIME_ENV", "production").strip().lower()
    if runtime == "production" and mode in {"development", "demo"}:
        raise RuntimeError("development/demo local auth cannot be enabled in production")
    if mode == "disabled":
        return
    username = os.getenv("BLANK_ADMIN_USERNAME", "admin")
    with SessionLocal() as db:
        configured = db.query(Account).filter(Account.username == username).one_or_none()
        if mode in {"enabled", "break_glass"}:
            now = datetime.now(UTC)
            if configured is not None:
                if (
                    account_is_eligible(configured, now=now)
                    and configured.external_source is None
                    and configured.is_admin
                ):
                    return
                raise RuntimeError("configured bootstrap username is not a usable local superadmin")
            usable_admin = (
                db.query(Account.id)
                .filter(
                    Account.external_source.is_(None),
                    Account.is_admin.is_(True),
                    Account.active.is_(True),
                    or_(Account.expires_at.is_(None), Account.expires_at > now),
                )
                .first()
            )
            if usable_admin is not None:
                return
        elif configured is not None:
            # development/demo 保持 v1 行为：配置用户名存在即不改写。
            return

        password = os.getenv("BLANK_ADMIN_PASSWORD")
        if not password or is_unsafe_bootstrap_secret(password, min_length=12):
            raise RuntimeError("BLANK_ADMIN_PASSWORD must be at least 12 characters and must not use a public example")
        db.add(
            Account(
                username=username,
                email=os.getenv("BLANK_ADMIN_EMAIL") or None,
                password_hash=pwd_context.hash(password),
                active=True,
                is_admin=True,
                must_change_password=True,
            )
        )
        db.commit()


def local_auth_mode() -> str:
    mode = os.getenv("BLANK_LOCAL_AUTH_MODE", "disabled").strip().lower()
    if mode not in {"disabled", "development", "demo", "enabled", "break_glass"}:
        raise RuntimeError("BLANK_LOCAL_AUTH_MODE must be disabled, development, demo, enabled, or break_glass")
    return mode


def account_is_eligible(account: Account, *, now: datetime | None = None) -> bool:
    """本地资格判定唯一实现；外部身份仅受 active 约束，不读取本地模式。"""

    if not account.active:
        return False
    if account.external_source:
        return True
    current = now or datetime.now(UTC)
    expires_at = account.expires_at
    if expires_at is not None and (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)) <= current:
        return False
    mode = local_auth_mode()
    if mode == "disabled":
        return False
    if mode == "break_glass":
        return bool(account.is_admin)
    return True


def is_local_superadmin(account: Account) -> bool:
    """本地超管规范谓词；禁止调用方仅凭 is_admin 推断。"""

    return account.external_source is None and bool(account.is_admin) and local_auth_mode() != "disabled"


def security_capabilities(account: Account) -> SecurityCapabilities:
    """本地安全操作能力的唯一事实源:`/auth/me` 声明与后端放行都只读它。

    只有「启用本地认证 + 拥有本地口令 + 非外部身份」的账号可以管理本地凭据。
    本地非管理员也拥有合同约定的自助安全能力。
    """

    allowed = bool(account.password_hash and not account.external_source and local_auth_mode() != "disabled")
    return SecurityCapabilities(
        password_change=allowed,
        totp_status=allowed,
        totp_enroll=allowed,
        totp_disable=allowed,
        passkey_list=allowed,
        passkey_register=allowed,
        passkey_delete=allowed,
    )


def authorize_security_operation(user: CurrentUser, operation: str) -> None:
    """按当前用户与具体操作 fail-closed 判定本地安全操作;未知操作一律拒绝。"""

    capability = SECURITY_OPERATION_CAPABILITIES.get(operation)
    if capability is None:
        raise AuthError(403, "未知的本地安全操作")
    with SessionLocal() as db:
        account = db.get(Account, user.id)
        if account is None or not account.active:
            raise AuthError(401, "登录态无效")
        if not getattr(security_capabilities(account), capability):
            raise AuthError(403, "本地认证管理未启用")


def _get_setting(key: str) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.get(PlatformSetting, key)
        return dict(row.value) if row else {}


def _save_setting(key: str, value: dict[str, Any], *, actor_id: str, action: str) -> None:
    with SessionLocal() as db:
        row = db.get(PlatformSetting, key) or PlatformSetting(key=key)
        before = _redacted_setting(row.value if row.value else {})
        row.value = value
        db.add(row)
        db.add(
            PlatformAuditLog(
                actor_id=actor_id,
                action=action,
                before_data=before,
                after_data=_redacted_setting(value),
            )
        )
        db.commit()


def _allow_local_outbound() -> bool:
    return os.getenv("BLANK_ALLOW_LOCAL_OUTBOUND", "false").lower() in {"1", "true", "yes"}


def _decrypt_saved_secret(value: Any) -> str:
    return decrypt_secret(str(value or ""))


def _redacted_setting(value: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(value)
    for key in ("client_secret", "authentik_api_token", "credential"):
        if key in redacted:
            redacted[key] = "[configured]" if redacted[key] else ""
    return redacted


def record_platform_audit(
    actor_id: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    def bounded_text(value: object, limit: int) -> str:
        """在唯一审计写边界收敛 ORM varchar 字段，避免调用方各自猜测列宽。"""

        return str(value).strip()[:limit]

    with SessionLocal() as db:
        db.add(
            PlatformAuditLog(
                actor_id=bounded_text(actor_id, 100),
                action=bounded_text(action, 160),
                before_data=jsonable_encoder(_redacted_setting(before or {})) if before is not None else None,
                after_data=jsonable_encoder(_redacted_setting(after or {})) if after is not None else None,
            )
        )
        db.commit()


def record_login_audit(actor_id: str, action: str, method: str) -> None:
    """登录审计只记录模式与方法；写失败不得改变认证响应。"""

    try:
        _facade().record_platform_audit(
            actor_id,
            action,
            None,
            {"mode": local_auth_mode(), "method": method},
        )
    except Exception:
        logger.exception("login audit write failed")
