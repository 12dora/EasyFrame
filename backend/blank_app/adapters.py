"""blank host 对共享 enterprise_platform ports 的 PostgreSQL 适配。"""

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
    UserSyncCapabilityResponse,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret, encrypt_secret
from enterprise_platform.trusted_http import create_trusted_authority_transport

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
TIMING_EQUALIZER_HASH = pwd_context.hash("enterprise-platform-timing-equalizer")
logger = logging.getLogger(__name__)
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


def _passkey_config() -> shared_passkeys.PasskeyConfig:
    # Passkey 挑战 TTL 只有 5 分钟,轮换期不保留历史密钥,过期挑战由前端重试即可。
    return shared_passkeys.PasskeyConfig(
        rp_id=os.getenv("BLANK_WEBAUTHN_RP_ID", "localhost"),
        rp_name=os.getenv("BLANK_WEBAUTHN_RP_NAME", "Enterprise Platform"),
        origins=tuple(
            item.strip()
            for item in os.getenv("BLANK_WEBAUTHN_ORIGINS", "http://localhost:3000").split(",")
            if item.strip()
        ),
        signing_secret=signing_key(PASSKEY_STATE_KEY_PURPOSE),
    )


def _passkey_challenge_key(jti: str) -> str:
    return f"{_PASSKEY_CHALLENGE_KEY_PREFIX}{shared_passkeys.hash_jti(jti)}"


def _persist_passkey_challenge(db, *, account_id: str, purpose: str, issued) -> None:
    db.execute(
        delete(PlatformSetting).where(
            PlatformSetting.key.like(f"{_PASSKEY_CHALLENGE_KEY_PREFIX}%"),
            PlatformSetting.value["expires_at"].as_integer() < int(datetime.now(UTC).timestamp()),
        )
    )
    db.add(
        PlatformSetting(
            key=_passkey_challenge_key(issued.jti),
            value={
                "account_id": str(account_id),
                "purpose": purpose,
                "expires_at": int(issued.expires_at),
                "consumed_at": None,
            },
        )
    )
    db.commit()


def _consume_passkey_challenge(db, *, account_id: str, purpose: str, jti: str, now: datetime) -> bool:
    """Atomically mark one durable challenge consumed in the caller's transaction."""

    consumed_at = now.isoformat()
    result = db.execute(
        update(PlatformSetting)
        .where(
            PlatformSetting.key == _passkey_challenge_key(jti),
            PlatformSetting.value["account_id"].as_string() == str(account_id),
            PlatformSetting.value["purpose"].as_string() == purpose,
            PlatformSetting.value["expires_at"].as_integer() >= int(now.timestamp()),
            PlatformSetting.value["consumed_at"].as_string().is_(None),
        )
        .values(
            value={
                "account_id": str(account_id),
                "purpose": purpose,
                "expires_at": int(now.timestamp()),
                "consumed_at": consumed_at,
            }
        )
    )
    return result.rowcount == 1


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
                if account_is_eligible(configured, now=now) and configured.external_source is None and configured.is_admin:
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


def _decode_session_token(token: str) -> dict[str, Any]:
    """解析 session token;轮换期依次尝试当前与历史根密钥派生的 session 密钥。"""

    for key in verification_keys(SESSION_KEY_PURPOSE):
        try:
            return jwt.decode(token, key, algorithms=[JWT_ALGORITHM])
        except JWTError:
            continue
    raise AuthError(401, "登录态无效")


def _account_projection(account: Account, *, has_passkey: bool) -> LocalAccount:
    return LocalAccount(
        id=str(account.id),
        name=account.username,
        email=account.email,
        avatar_url=account.avatar_url,
        ui_locale=account.ui_locale,
        active=account.active,
        is_local_superuser=account.is_admin,
        must_change_password=account.must_change_password,
        totp_enabled=account.totp_enabled,
        has_passkey=has_passkey,
    )


class BlankAccountAdapter:
    def authenticate_password(self, username: str, password: str) -> LocalAccount | None:
        with SessionLocal() as db:
            account = (
                db.query(Account)
                .filter(Account.username == username, Account.external_source.is_(None))
                .one_or_none()
            )
            candidate_hash = (
                account.password_hash if account is not None and account.password_hash else TIMING_EQUALIZER_HASH
            )
            try:
                verified = pwd_context.verify(password, candidate_hash)
            except (TypeError, ValueError):
                verified = False
            # 无真实 hash 时仍完整执行一次 bcrypt 以拉平时序，但 dummy hash 的
            # 校验结果绝不能成为账号凭据。
            password_matches = verified and bool(account is not None and account.password_hash)
            if account is None or not password_matches or not account_is_eligible(account):
                return None
            has_passkey = db.query(Passkey.id).filter(Passkey.account_id == account.id).first() is not None
            return _account_projection(account, has_passkey=has_passkey)

    def verify_totp(self, account_id: str, code: str) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            return bool(
                account
                and account_is_eligible(account)
                and account.totp_enabled
                and account.totp_secret
                and pyotp.TOTP(account.totp_secret).verify(code)
            )

    def issue_session(self, account_id: str) -> str:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account is None or not account_is_eligible(account):
                raise AuthError(401, "登录态无效")
            auth_source = "external" if account.external_source else "local"
        return jwt.encode(
            {
                "sub": account_id,
                "jti": uuid.uuid4().hex,
                "auth_source": auth_source,
                "iat": now,
                "session_started_at": now.timestamp(),
                "exp": now + timedelta(hours=8),
            },
            signing_key(SESSION_KEY_PURPOSE),
            JWT_ALGORITHM,
        )

    def _authenticated_account(self) -> tuple[Account, bool]:
        principal_account_id = request_principal_account_id.get()
        if principal_account_id:
            with SessionLocal() as db:
                account = db.get(Account, principal_account_id)
                if account is None or not account_is_eligible(account):
                    raise AuthError(401, "登录态无效")
                has_passkey = db.query(Passkey.id).filter(Passkey.account_id == account.id).first() is not None
                db.expunge(account)
                return account, has_passkey
        token = request_token.get()
        if not token:
            raise AuthError(401, "未登录")
        claims = _decode_session_token(token)
        with SessionLocal() as db:
            account = db.get(Account, claims.get("sub"))
            if account is None or not account_is_eligible(account):
                raise AuthError(401, "登录态无效")
            issued_at = datetime.fromtimestamp(float(claims.get("session_started_at", claims["iat"])), tz=UTC)
            revoked_at = account.sessions_revoked_at
            if revoked_at and (revoked_at if revoked_at.tzinfo else revoked_at.replace(tzinfo=UTC)) >= issued_at:
                raise AuthError(401, "登录态已失效")
            has_passkey = db.query(Passkey.id).filter(Passkey.account_id == account.id).first() is not None
            db.expunge(account)
            return account, has_passkey

    def current_user(self) -> CurrentUser:
        account, _ = self._authenticated_account()
        if is_local_superadmin(account):
            grants = _superadmin_grants()
            permissions = set(ALL_PERMISSIONS)
        elif not account.external_source:
            grants = _local_grants(account)
            permissions = {grant.code for grant in grants}
        else:
            grants = _snapshot_grants(account)
            permissions = {grant.code for grant in grants}
        return CurrentUser(
            id=str(account.id),
            account_id=str(account.id),
            is_local_superadmin=is_local_superadmin(account),
            name=account.username,
            email=account.email,
            avatar_url=account.avatar_url,
            ui_locale=account.ui_locale,
            must_change_password=account.must_change_password,
            has_local_password=bool(account.password_hash),
            permissions=sorted(permissions),
            grants=list(grants),
            role_groups=_snapshot_role_groups(account),
            security_capabilities=security_capabilities(account),
        )

    def revoke_sessions(self, account_id: str) -> None:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account:
                account.sessions_revoked_at = datetime.now(UTC)
                db.commit()

    def change_password(self, account_id: str, current_password: str, new_password: str) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if (
                account is None
                or not account.password_hash
                or not pwd_context.verify(current_password, account.password_hash)
            ):
                return False
            # 超管自助改密沿用 bootstrap 强度(≥12+弱值黑名单),防首登改密降级(06 §2)。
            if account.is_admin and is_unsafe_bootstrap_secret(new_password, min_length=12):
                raise AuthError(422, "管理员密码不符合强度要求")
            account.password_hash = pwd_context.hash(new_password)
            account.must_change_password = False
            account.sessions_revoked_at = datetime.now(UTC)
            db.commit()
            return True

    def totp_status(self, account_id: str) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            return bool(account and account.totp_enabled)

    def totp_begin(self, account_id: str) -> tuple[str, str]:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account is None or account.totp_enabled:
                raise AuthError(409, "TOTP 已启用, 请先禁用")
            secret = pyotp.random_base32()
            account.totp_pending_secret = secret
            db.commit()
            return secret, pyotp.TOTP(secret).provisioning_uri(account.username, issuer_name="Enterprise Platform")

    def totp_confirm(self, account_id: str, code: str) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if (
                account is None
                or not account.totp_pending_secret
                or not pyotp.TOTP(account.totp_pending_secret).verify(code)
            ):
                return False
            account.totp_secret = account.totp_pending_secret
            account.totp_pending_secret = None
            account.totp_enabled = True
            account.sessions_revoked_at = datetime.now(UTC)
            db.commit()
            return True

    def totp_disable(self, account_id: str, password: str, code: str) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if (
                account is None
                or not account.totp_secret
                or not pwd_context.verify(password, account.password_hash)
                or not pyotp.TOTP(account.totp_secret).verify(code)
            ):
                return False
            account.totp_enabled = False
            account.totp_secret = None
            account.totp_pending_secret = None
            account.sessions_revoked_at = datetime.now(UTC)
            db.commit()
            return True

    def list_passkeys(self, account_id: str) -> list[PasskeySummary]:
        with SessionLocal() as db:
            rows = db.query(Passkey).filter(Passkey.account_id == account_id).order_by(Passkey.created_at).all()
            return [PasskeySummary.model_validate(row) for row in rows]

    def begin_passkey_login(self, account_id: str) -> PasskeyChallenge:
        with SessionLocal() as db:
            rows = db.query(Passkey).filter(Passkey.account_id == account_id).all()
            config = _passkey_config()
            try:
                options, issued = shared_passkeys.begin_authentication(
                    user_id=account_id,
                    credential_ids=[row.credential_id for row in rows],
                    config=config,
                )
            except shared_passkeys.PasskeyError as exc:
                raise AuthError(exc.status_code, exc.detail) from exc
            _persist_passkey_challenge(
                db,
                account_id=account_id,
                purpose=config.authenticate_purpose,
                issued=issued,
            )
        return PasskeyChallenge(options=options, state_token=issued.token)

    def complete_passkey_login(self, account_id: str, state_token: str, credential: dict[str, Any]) -> bool:
        try:
            credential_id = shared_passkeys.credential_id(credential)
        except shared_passkeys.PasskeyError as exc:
            raise AuthError(exc.status_code, exc.detail) from exc
        config = _passkey_config()
        now = datetime.now(UTC)
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account is None or not account_is_eligible(account, now=now):
                return False
            row = (
                db.query(Passkey)
                .filter(Passkey.account_id == account_id, Passkey.credential_id == credential_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                return False
            try:
                row.sign_count = shared_passkeys.complete_authentication(
                    user_id=account_id,
                    state_token=state_token,
                    credential=credential,
                    public_key=row.public_key,
                    current_sign_count=row.sign_count,
                    config=config,
                    challenge_consumer=lambda jti: _consume_passkey_challenge(
                        db,
                        account_id=account_id,
                        purpose=config.authenticate_purpose,
                        jti=jti,
                        now=now,
                    ),
                )
            except shared_passkeys.PasskeyError as exc:
                db.rollback()
                if exc.status_code in {400, 401} and "挑战" in str(exc.detail):
                    return False
                raise AuthError(exc.status_code, exc.detail) from exc
            row.last_used_at = now
            db.commit()
            return True

    def begin_passkey_registration(self, account_id: str) -> PasskeyChallenge:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account is None:
                raise AuthError(401, "登录态无效")
            config = _passkey_config()
            options, issued = shared_passkeys.begin_registration(
                user_id=account_id,
                username=account.username,
                config=config,
            )
            _persist_passkey_challenge(
                db,
                account_id=account_id,
                purpose=config.register_purpose,
                issued=issued,
            )
        return PasskeyChallenge(options=options, state_token=issued.token)

    def complete_passkey_registration(
        self, account_id: str, state_token: str, credential: dict[str, Any], name: str
    ) -> PasskeySummary:
        config = _passkey_config()
        now = datetime.now(UTC)
        with SessionLocal() as db:
            try:
                result = shared_passkeys.complete_registration(
                    user_id=account_id,
                    state_token=state_token,
                    credential=credential,
                    config=config,
                    challenge_consumer=lambda jti: _consume_passkey_challenge(
                        db,
                        account_id=account_id,
                        purpose=config.register_purpose,
                        jti=jti,
                        now=now,
                    ),
                )
            except shared_passkeys.PasskeyError as exc:
                db.rollback()
                raise AuthError(exc.status_code, exc.detail) from exc
            if db.query(Passkey.id).filter(Passkey.credential_id == result.credential_id).first() is not None:
                db.rollback()
                raise AuthError(409, "该通行密钥已注册")
            row = Passkey(
                account_id=account_id,
                credential_id=result.credential_id,
                public_key=result.public_key,
                sign_count=result.sign_count,
                name=name,
            )
            db.add(row)
            account = db.get(Account, account_id)
            if account is not None:
                account.sessions_revoked_at = now
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise AuthError(409, "该通行密钥已注册") from exc
            db.refresh(row)
            return PasskeySummary.model_validate(row)

    def delete_passkey(self, account_id: str, passkey_id: str) -> bool:
        try:
            parsed_passkey_id = uuid.UUID(passkey_id)
        except ValueError:
            return False
        with SessionLocal() as db:
            row = (
                db.query(Passkey)
                .filter(Passkey.account_id == account_id, Passkey.id == parsed_passkey_id)
                .one_or_none()
            )
            if row is None:
                return False
            db.delete(row)
            account = db.get(Account, account_id)
            if account is not None:
                account.sessions_revoked_at = datetime.now(UTC)
            db.commit()
            return True


class BlankLocalAccountUnitOfWork:
    """SQLAlchemy 事务适配；退出上下文时业务写入与审计只提交一次。"""

    def __init__(self) -> None:
        self.db = SessionLocal()

    def __enter__(self) -> BlankLocalAccountUnitOfWork:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.db.close()
        return False

    @staticmethod
    def _record(account: Account, *, passkey_count: int = 0) -> LocalAccountRecord:
        return LocalAccountRecord(
            id=account.id,
            username=account.username,
            email=account.email,
            active=account.active,
            is_admin=account.is_admin,
            local_permissions=list(account.local_permissions or []),
            local_grants_version=account.local_grants_version,
            expires_at=account.expires_at,
            must_change_password=account.must_change_password,
            ui_locale=account.ui_locale,
            totp_enabled=account.totp_enabled,
            totp_secret=account.totp_secret,
            totp_pending_secret=account.totp_pending_secret,
            created_at=account.created_at,
            passkey_count=passkey_count,
        )

    def list_accounts(self, *, search: str | None) -> list[LocalAccountRecord]:
        passkey_counts = (
            self.db.query(Passkey.account_id, func.count(Passkey.id)).group_by(Passkey.account_id).subquery()
        )
        query = (
            self.db.query(Account, func.coalesce(passkey_counts.c.count, 0))
            .outerjoin(passkey_counts, passkey_counts.c.account_id == Account.id)
            .filter(Account.external_source.is_(None))
        )
        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.filter(or_(Account.username.ilike(term), Account.email.ilike(term)))
        rows = query.order_by(Account.created_at.asc(), Account.username.asc()).all()
        return [self._record(account, passkey_count=int(count or 0)) for account, count in rows]

    def get_account(self, account_id: uuid.UUID, *, for_update: bool = False) -> LocalAccountRecord | None:
        query = self.db.query(Account).filter(
            Account.id == account_id,
            Account.external_source.is_(None),
        )
        if for_update:
            query = query.with_for_update()
        account = query.one_or_none()
        if account is None:
            return None
        passkey_count = int(
            self.db.query(func.count(Passkey.id)).filter(Passkey.account_id == account.id).scalar() or 0
        )
        return self._record(account, passkey_count=passkey_count)

    def permission_catalog(self) -> list[LocalPermissionRecord]:
        rows = self.db.query(PermissionCatalog).order_by(PermissionCatalog.code.asc()).all()
        return [
            LocalPermissionRecord(
                code=row.code,
                name_zh=row.name_zh,
                name_en=row.name_en,
                domain=row.domain,
                group_key=row.group_key,
                supported_scopes=list(row.supported_scopes or []),
                risk_level=row.risk_level,
                active=row.active,
            )
            for row in rows
        ]

    def lock_local_admins(self) -> None:
        # 所有可能影响最后管理员不变式的事务先按主键顺序锁住同一组行。
        self.db.query(Account.id).filter(
            Account.external_source.is_(None),
            Account.is_admin.is_(True),
        ).order_by(Account.id).with_for_update().all()

    def usable_local_admin_count(self, *, now: datetime) -> int:
        self.db.flush()
        return int(
            self.db.query(func.count(Account.id))
            .filter(
                Account.external_source.is_(None),
                Account.is_admin.is_(True),
                Account.active.is_(True),
                or_(Account.expires_at.is_(None), Account.expires_at > now),
            )
            .scalar()
            or 0
        )

    def create_account(self, **values: Any) -> LocalAccountRecord:
        password = values.pop("password")
        account = Account(password_hash=pwd_context.hash(password), **values)
        self.db.add(account)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise AuthError(409, "用户名已被占用") from exc
        return self._record(account)

    def update_account(self, account_id: uuid.UUID, **values: Any) -> LocalAccountRecord:
        account = self.db.get(Account, account_id)
        if account is None:
            raise AuthError(404, "本地账户不存在")
        password = values.pop("password", None)
        if password is not None:
            account.password_hash = pwd_context.hash(password)
        for key, value in values.items():
            setattr(account, key, value)
        self.db.flush()
        passkey_count = int(
            self.db.query(func.count(Passkey.id)).filter(Passkey.account_id == account.id).scalar() or 0
        )
        return self._record(account, passkey_count=passkey_count)

    def delete_account(self, account_id: uuid.UUID) -> None:
        account = self.db.get(Account, account_id)
        if account is None:
            raise AuthError(404, "本地账户不存在")
        self.db.query(Passkey).filter(Passkey.account_id == account.id).delete()
        self.db.query(Notification).filter(Notification.account_id == account.id).delete()
        self.db.query(PermissionSnapshot).filter(PermissionSnapshot.account_id == account.id).delete()
        self.db.delete(account)
        self.db.flush()

    def append_audit(
        self,
        *,
        actor_id: str,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        self.db.add(
            PlatformAuditLog(
                actor_id=str(actor_id).strip()[:100],
                action=str(action).strip()[:160],
                before_data=jsonable_encoder(_redacted_setting(before or {})) if before is not None else None,
                after_data=jsonable_encoder(_redacted_setting(after or {})) if after is not None else None,
            )
        )
        self.db.flush()


class BlankLocalAccountAdmin(LocalAccountAdminPort):
    def unit_of_work(self) -> BlankLocalAccountUnitOfWork:
        return BlankLocalAccountUnitOfWork()


class BlankFooterAdapter:
    def get_footer(self) -> FooterSettings:
        with SessionLocal() as db:
            row = db.get(PlatformSetting, "footer")
            if row is None:
                return FooterSettings(footer_html_zh="企业应用 · © {year}", footer_html_en="Enterprise App · © {year}")
            return FooterSettings.model_validate(row.value)

    def save_footer(self, footer: FooterSettings, *, actor_id: str) -> FooterSettings:
        safe = FooterSettings(
            footer_html_zh=sanitize_footer_html(footer.footer_html_zh),
            footer_html_en=sanitize_footer_html(footer.footer_html_en),
        )
        with SessionLocal() as db:
            row = db.get(PlatformSetting, "footer") or PlatformSetting(key="footer")
            before = dict(row.value) if row.value else None
            row.value = safe.model_dump(mode="json")
            db.add(row)
            db.add(
                PlatformAuditLog(
                    actor_id=actor_id,
                    action="settings.footer.update",
                    before_data=before,
                    after_data=safe.model_dump(mode="json"),
                )
            )
            db.commit()
        return safe


class BlankNotificationAdapter:
    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        with SessionLocal() as db:
            query = db.query(Notification).filter(Notification.account_id == user_id)
            if cursor:
                created_at, notification_id = _decode_notification_cursor(cursor)
                query = query.filter(
                    or_(
                        Notification.created_at < created_at,
                        and_(Notification.created_at == created_at, Notification.id < notification_id),
                    )
                )
            rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1).all()
            items = [
                NotificationItem(
                    id=str(row.id),
                    title=row.title,
                    body=row.body,
                    level=row.level,
                    created_at=row.created_at,
                    read_at=row.read_at,
                    href=row.href,
                )
                for row in rows[:limit]
            ]
            unread = (
                db.query(Notification.id)
                .filter(Notification.account_id == user_id, Notification.read_at.is_(None))
                .count()
            )
            return NotificationPage(
                items=items,
                unread_count=unread,
                next_cursor=_encode_notification_cursor(rows[limit - 1]) if len(rows) > limit else None,
            )

    def mark_read(self, user_id: str, notification_id: str, *, read_at: datetime) -> bool:
        with SessionLocal() as db:
            row = (
                db.query(Notification)
                .filter(Notification.account_id == user_id, Notification.id == notification_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.read_at = read_at
            db.commit()
            return True

    def mark_all_read(self, user_id: str, *, read_at: datetime) -> int:
        with SessionLocal() as db:
            count = (
                db.query(Notification)
                .filter(Notification.account_id == user_id, Notification.read_at.is_(None))
                .update({Notification.read_at: read_at})
            )
            db.commit()
            return count


def _encode_notification_cursor(row: Notification) -> str:
    payload = f"{_as_utc(row.created_at).isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_notification_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        created_at_text, notification_id_text = decoded.rsplit("|", 1)
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return created_at.astimezone(UTC), uuid.UUID(notification_id_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthError(400, "通知游标无效") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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


class BlankIntegrationAdapter:
    def get_oidc_settings(self) -> OidcSettings:
        data = _get_setting("oidc")
        return OidcSettings.model_validate({**data, "user_sync_supported": False})

    def save_oidc_settings(self, payload: OidcSettingsUpdate, *, actor_id: str) -> OidcSettings:
        payload = normalize_oidc_settings(payload)
        old = _get_setting("oidc")
        old_client_authority = oidc_client_authority(old)
        new_client_authority = oidc_client_authority(payload)
        client_authority_changed = old_client_authority.has_values() and old_client_authority != new_client_authority
        api_authority_changed = bool(old.get("authentik_api_base_url")) and (
            normalize_base_url(str(old.get("authentik_api_base_url") or "")) != payload.authentik_api_base_url
        )
        client_secret = payload.client_secret
        if client_authority_changed and not client_secret:
            raise AuthError(422, "身份提供方 authority 或 clientId 变更时必须重新填写 clientSecret")
        if client_secret is None:
            client_secret = _decrypt_saved_secret(old.get("client_secret"))
        authentik_api_token = payload.authentik_api_token
        if api_authority_changed and not authentik_api_token:
            raise AuthError(422, "Authentik API authority 变更时必须重新填写 API token")
        if authentik_api_token is None:
            authentik_api_token = _decrypt_saved_secret(old.get("authentik_api_token"))
        data = payload.model_dump(mode="json", exclude={"client_secret", "authentik_api_token"})
        data["has_client_secret"] = bool(client_secret)
        data["has_authentik_api_token"] = bool(authentik_api_token)
        data["client_secret"] = encrypt_secret(client_secret or "")
        data["authentik_api_token"] = encrypt_secret(authentik_api_token or "")
        data["redirect_uri"] = (
            f"{payload.redirect_base_url.rstrip('/')}/api/v1/auth/oidc/callback" if payload.redirect_base_url else ""
        )
        _save_setting("oidc", data, actor_id=actor_id, action="identity.settings.update")
        return OidcSettings.model_validate({**data, "user_sync_supported": False})

    def test_oidc(self) -> ConnectionTestResult:
        configured = self.get_oidc_settings()
        server_base_url = configured.server_base_url.strip()
        return probe_jwks(
            rewrite_for_server_side(server_base_url, configured.jwks_uri),
            allow_localhost=_allow_local_outbound(),
            transport=(create_trusted_authority_transport(server_base_url) if server_base_url else guarded_request),
        )

    def discover_oidc(self, issuer: str | None) -> IdentityDiscoveryResponse:
        configured = self.get_oidc_settings()
        target = (issuer or configured.issuer).strip().rstrip("/")
        if not target:
            return IdentityDiscoveryResponse(ok=False, error_kind="not_configured", error_detail="issuer 不能为空")
        server_base_url = configured.server_base_url.strip()
        url = rewrite_for_server_side(server_base_url, f"{target}/.well-known/openid-configuration")
        transport = create_trusted_authority_transport(server_base_url) if server_base_url else guarded_request
        try:
            response = transport("GET", url, timeout=5, allow_localhost=_allow_local_outbound())
        except UnsafeOutboundUrlError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="blocked", error_detail=str(exc))
        except httpx.HTTPError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="unreachable", error_detail=str(exc)[:300])
        if response.status_code != 200:
            return IdentityDiscoveryResponse(
                ok=False, error_kind="http_error", error_detail=f"discovery 返回 {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            return IdentityDiscoveryResponse(ok=False, error_kind="invalid_response", error_detail="响应不是 JSON")
        required = ("authorization_endpoint", "token_endpoint", "jwks_uri")
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
            return IdentityDiscoveryResponse(
                ok=False, error_kind="invalid_response", error_detail="discovery 缺少必要端点"
            )
        try:
            for key in required:
                from enterprise_platform.urls import validate_endpoint_url

                validate_endpoint_url(str(payload[key]))
        except ValueError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="invalid_response", error_detail=str(exc))
        return IdentityDiscoveryResponse(
            ok=True,
            issuer=str(payload.get("issuer") or target),
            authorization_endpoint=str(payload["authorization_endpoint"]),
            token_endpoint=str(payload["token_endpoint"]),
            jwks_uri=str(payload["jwks_uri"]),
            userinfo_endpoint=str(payload.get("userinfo_endpoint") or ""),
        )

    def sync_identity_users(self, *, actor_id: str) -> UserSyncCapabilityResponse:
        return UserSyncCapabilityResponse(
            supported=False,
            status="not_supported",
            summary="blank host 仅在登录时维护最小用户投影，不提供权威目录生命周期同步",
        )

    def get_easyauth_status(self) -> EasyAuthStatus:
        data = _get_setting("easyauth")
        return EasyAuthStatus.model_validate(data or {})

    def save_easyauth_settings(self, payload: EasyAuthSettingsUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _get_setting("easyauth")
        authority_changed = bool(old.get("base_url") or old.get("app_key")) and (
            old.get("base_url", "").rstrip("/") != payload.base_url.rstrip("/")
            or old.get("app_key", "") != payload.app_key
        )
        credential = payload.credential
        if authority_changed and not credential:
            raise AuthError(422, "EasyAuth authority 或 appKey 变更时必须重新填写 credential")
        if credential is None:
            credential = _decrypt_saved_secret(old.get("credential"))
        credential = credential or ""
        data = {
            "configured": bool(payload.base_url and payload.app_key and credential),
            "base_url": payload.base_url.rstrip("/"),
            "app_key": payload.app_key,
            "auth_mode": "static_app_token",
            "has_credential": bool(credential),
            "permission_request_url": payload.permission_request_url,
            "credential": encrypt_secret(credential),
        }
        _save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return EasyAuthStatus.model_validate(data)

    def save_permission_request_url(self, payload: PermissionRequestUrlUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _get_setting("easyauth")
        data = dict(old)
        data["permission_request_url"] = payload.permission_request_url.strip()
        _save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return EasyAuthStatus.model_validate(data)

    def test_easyauth(self) -> ConnectionTestResult:
        data = _get_setting("easyauth")
        try:
            credential = decrypt_secret(str(data.get("credential") or ""))
        except SecretConfigurationError as exc:
            return ConnectionTestResult(ok=False, error_kind="configuration", error_detail=str(exc))
        client = EasyAuthPermissionClient(
            base_url=str(data.get("base_url") or ""),
            app_key=str(data.get("app_key") or "enterprise-blank"),
            auth_mode=str(data.get("auth_mode") or "static_app_token"),
            credential=credential,
            timeout=5,
        )
        try:
            snapshot = client.fetch_permission_snapshot("enterprise-platform-connectivity-probe")
        except EasyAuthForbiddenError as exc:
            kind = classify_connection_failure(str(exc), forbidden=True)
            return ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        except EasyAuthClientError as exc:
            kind = classify_connection_failure(
                str(exc),
                network_error=isinstance(exc.__cause__, httpx.RequestError),
            )
            return ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        finally:
            client.close()
        return ConnectionTestResult(ok=True, error_detail=f"snapshot {snapshot.snapshot_version}")


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
        record_platform_audit(
            actor_id,
            action,
            None,
            {"mode": local_auth_mode(), "method": method},
        )
    except Exception:
        logger.exception("login audit write failed")


class BlankUpstreamHealthAdapter:
    probes = {
        "authentik": ("Authentik(SSO 登录)", True, lambda: BlankIntegrationAdapter().test_oidc()),
        "authentik_directory": ("Authentik 用户同步", False, None),
        "easyauth": ("EasyAuth(权限授权)", True, lambda: BlankIntegrationAdapter().test_easyauth()),
        "scheduler": ("定时任务调度器", False, None),
    }

    def latest(self) -> list[UpstreamHealthItem]:
        with SessionLocal() as db:
            result: list[UpstreamHealthItem] = []
            for dependency, (display_name, supported, _) in self.probes.items():
                row = (
                    db.query(HealthSnapshot)
                    .filter(HealthSnapshot.dependency == dependency)
                    .order_by(HealthSnapshot.checked_at.desc())
                    .first()
                )
                result.append(
                    UpstreamHealthItem(
                        dependency=dependency,
                        display_name=display_name,
                        status=row.status if row else "unknown",
                        checked_at=row.checked_at if row else None,
                        summary=safe_health_summary(row.summary) if row else "尚未记录健康快照",
                        error_summary=safe_health_summary(row.error_summary) if row else "",
                        summary_code=(_health_summary_code(row.status, row.summary) if row else "upstream.not_checked")
                        if supported
                        else "upstream.not_supported",
                        supported=supported,
                    )
                )
            return result

    def run_checks(self, *, actor_id: str) -> list[UpstreamHealthItem]:
        with SessionLocal() as db:
            for dependency, (display_name, supported, probe) in self.probes.items():
                if not supported or probe is None:
                    result = ConnectionTestResult(
                        ok=False, error_kind="not_supported", error_detail="capability is not supported by this host"
                    )
                else:
                    result = probe()
                db.add(
                    HealthSnapshot(
                        dependency=dependency,
                        display_name=display_name,
                        status="healthy"
                        if result.ok
                        else ("unknown" if result.error_kind in {"not_configured", "not_supported"} else "unhealthy"),
                        summary=(
                            "连接正常"
                            if result.ok
                            else ("当前宿主不支持此能力" if not supported else f"连接测试失败({result.error_kind})")
                        ),
                        error_summary=result.error_detail or "",
                    )
                )
            db.commit()
        return self.latest()


def _health_summary_code(status: str, summary: str) -> str:
    if "不支持" in summary:
        return "upstream.not_supported"
    if status == "healthy":
        return "upstream.healthy"
    if status == "warning":
        return "upstream.warning"
    if status == "unhealthy":
        return "upstream.unhealthy"
    return "upstream.unknown"


account_adapter = BlankAccountAdapter()
local_account_admin = BlankLocalAccountAdmin()


def require_permission(code: str) -> None:
    user = account_adapter.current_user()
    if code not in user.permissions:
        raise AuthError(403, "缺少权限")


def _catalog_permissions(db) -> dict[str, CatalogPermission]:
    """目录坏 scope 逐项忽略，不能让一条脏配置扩大权限或毒化整次读取。"""

    catalog: dict[str, CatalogPermission] = {}
    for row in db.query(PermissionCatalog).all():
        scopes: set[DataScope] = set()
        for raw_scope in row.supported_scopes if isinstance(row.supported_scopes, list | tuple) else ():
            try:
                scopes.add(DataScope(str(raw_scope)))
            except ValueError:
                continue
        catalog[row.code] = CatalogPermission(
            code=row.code,
            supported_scopes=frozenset(scopes),
            active=row.active,
        )
    return catalog


def _snapshot_grants(account: Account) -> tuple[NormalizedGrant, ...]:
    if not account.external_source or not account.external_user_id:
        return ()
    with SessionLocal() as db:
        integration = db.get(PlatformSetting, "easyauth")
        app_key = str((integration.value if integration else {}).get("app_key") or "")
        snapshot = (
            db.query(PermissionSnapshot)
            .filter(
                PermissionSnapshot.external_source == account.external_source,
                PermissionSnapshot.external_user_id == account.external_user_id,
                PermissionSnapshot.app_key == app_key,
            )
            .one_or_none()
        )
        if snapshot is None:
            return ()
        expires_at = snapshot.expires_at
        if (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)) <= datetime.now(UTC):
            return ()
        return tuple(
            NormalizedGrant(code=grant.code, scope=grant.scope)
            for grant in normalize_grants(snapshot.grants, _catalog_permissions(db))
        )


def _local_grants(account: Account) -> tuple[NormalizedGrant, ...]:
    with SessionLocal() as db:
        catalog = _catalog_permissions(db)
    baseline = ({"code": code, "scope": "SELF"} for code in BASELINE_SELF_SERVICE)
    stored = account.local_permissions if isinstance(account.local_permissions, list) else []
    return normalize_local_grants((*baseline, *stored), catalog)


def _superadmin_grants() -> tuple[NormalizedGrant, ...]:
    """超管权限来自单一注册表；ALL 显式支配 SELF，不作 scope 序数比较。"""

    grants: list[NormalizedGrant] = []
    for permission in FRAMEWORK_PERMISSIONS:
        scopes = set(permission.supported_scopes) & {DataScope.SELF, DataScope.ALL}
        if DataScope.ALL in scopes:
            scope = DataScope.ALL
        elif DataScope.SELF in scopes:
            scope = DataScope.SELF
        else:
            continue
        grants.append(NormalizedGrant(code=permission.code, scope=scope))
    return tuple(grants)


def _snapshot_role_groups(account: Account) -> list[str]:
    if not account.external_source or not account.external_user_id:
        return []
    with SessionLocal() as db:
        integration = db.get(PlatformSetting, "easyauth")
        app_key = str((integration.value if integration else {}).get("app_key") or "").strip()
        if not app_key:
            return []
        snapshot = (
            db.query(PermissionSnapshot.groups)
            .filter(
                PermissionSnapshot.account_id == account.id,
                PermissionSnapshot.app_key == app_key,
                PermissionSnapshot.expires_at > datetime.now(UTC),
            )
            .order_by(PermissionSnapshot.fetched_at.desc())
            .first()
        )
    if snapshot is None or not isinstance(snapshot.groups, list):
        return []
    names: list[str] = []
    for group in snapshot.groups:
        name = group.get("name") if isinstance(group, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names
