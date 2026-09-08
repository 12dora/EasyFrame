"""blank host 账号端口、本地账户管理与权限投影。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from blank_app.models import (
    Account,
)
from enterprise_platform import passkeys as shared_passkeys
from enterprise_platform.authz import (
    CatalogPermission,
    DataScope,
    NormalizedGrant,
)
from enterprise_platform.local_accounts import (
    LocalAccountAdminPort,
    LocalAccountRecord,
    LocalPermissionRecord,
)
from enterprise_platform.ports import LocalAccount, PasskeyChallenge
from enterprise_platform.schemas import CurrentUser, PasskeySummary


def _facade():
    from blank_app import adapters

    return adapters


def _passkey_config() -> shared_passkeys.PasskeyConfig:
    # Passkey 挑战 TTL 只有 5 分钟,轮换期不保留历史密钥,过期挑战由前端重试即可。
    return _facade().shared_passkeys.PasskeyConfig(
        rp_id=_facade().os.getenv("BLANK_WEBAUTHN_RP_ID", "localhost"),
        rp_name=_facade().os.getenv("BLANK_WEBAUTHN_RP_NAME", "Enterprise Platform"),
        origins=tuple(
            item.strip()
            for item in _facade().os.getenv("BLANK_WEBAUTHN_ORIGINS", "http://localhost:3000").split(",")
            if item.strip()
        ),
        signing_secret=_facade().signing_key(_facade().PASSKEY_STATE_KEY_PURPOSE),
    )


def _passkey_challenge_key(jti: str) -> str:
    return f"{_facade()._PASSKEY_CHALLENGE_KEY_PREFIX}{_facade().shared_passkeys.hash_jti(jti)}"


def _persist_passkey_challenge(db, *, account_id: str, purpose: str, issued) -> None:
    db.execute(
        _facade()
        .delete(_facade().PlatformSetting)
        .where(
            _facade().PlatformSetting.key.like(f"{_facade()._PASSKEY_CHALLENGE_KEY_PREFIX}%"),
            _facade().PlatformSetting.value["expires_at"].as_integer()
            < int(_facade().datetime.now(_facade().UTC).timestamp()),
        )
    )
    db.add(
        _facade().PlatformSetting(
            key=_facade()._passkey_challenge_key(issued.jti),
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
        _facade()
        .update(_facade().PlatformSetting)
        .where(
            _facade().PlatformSetting.key == _facade()._passkey_challenge_key(jti),
            _facade().PlatformSetting.value["account_id"].as_string() == str(account_id),
            _facade().PlatformSetting.value["purpose"].as_string() == purpose,
            _facade().PlatformSetting.value["expires_at"].as_integer() >= int(now.timestamp()),
            _facade().PlatformSetting.value["consumed_at"].as_string().is_(None),
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


def _decode_session_token(token: str) -> dict[str, Any]:
    """解析 session token;轮换期依次尝试当前与历史根密钥派生的 session 密钥。"""

    for key in _facade().verification_keys(_facade().SESSION_KEY_PURPOSE):
        try:
            return _facade().jwt.decode(token, key, algorithms=[_facade().JWT_ALGORITHM])
        except _facade().JWTError:
            continue
    raise _facade().AuthError(401, "登录态无效")


def _account_projection(account: Account, *, has_passkey: bool) -> LocalAccount:
    return _facade().LocalAccount(
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
        with _facade().SessionLocal() as db:
            account = (
                db.query(_facade().Account)
                .filter(_facade().Account.username == username, _facade().Account.external_source.is_(None))
                .one_or_none()
            )
            candidate_hash = (
                account.password_hash
                if account is not None and account.password_hash
                else _facade().TIMING_EQUALIZER_HASH
            )
            try:
                verified = _facade().pwd_context.verify(password, candidate_hash)
            except (TypeError, ValueError):
                verified = False
            # 无真实 hash 时仍完整执行一次 bcrypt 以拉平时序，但 dummy hash 的
            # 校验结果绝不能成为账号凭据。
            password_matches = verified and bool(account is not None and account.password_hash)
            if account is None or not password_matches or not _facade().account_is_eligible(account):
                return None
            has_passkey = (
                db.query(_facade().Passkey.id).filter(_facade().Passkey.account_id == account.id).first() is not None
            )
            return _facade()._account_projection(account, has_passkey=has_passkey)

    def verify_totp(self, account_id: str, code: str) -> bool:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            return bool(
                account
                and _facade().account_is_eligible(account)
                and account.totp_enabled
                and account.totp_secret
                and _facade().pyotp.TOTP(account.totp_secret).verify(code)
            )

    def issue_session(self, account_id: str) -> str:
        now = _facade().datetime.now(_facade().UTC)
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if account is None or not _facade().account_is_eligible(account):
                raise _facade().AuthError(401, "登录态无效")
            auth_source = "external" if account.external_source else "local"
        return _facade().jwt.encode(
            {
                "sub": account_id,
                "jti": _facade().uuid.uuid4().hex,
                "auth_source": auth_source,
                "iat": now,
                "session_started_at": now.timestamp(),
                "exp": now + _facade().timedelta(hours=8),
            },
            _facade().signing_key(_facade().SESSION_KEY_PURPOSE),
            _facade().JWT_ALGORITHM,
        )

    def _authenticated_account(self) -> tuple[Account, bool]:
        principal_account_id = _facade().request_principal_account_id.get()
        if principal_account_id:
            with _facade().SessionLocal() as db:
                account = db.get(_facade().Account, principal_account_id)
                if account is None or not _facade().account_is_eligible(account):
                    raise _facade().AuthError(401, "登录态无效")
                has_passkey = (
                    db.query(_facade().Passkey.id).filter(_facade().Passkey.account_id == account.id).first()
                    is not None
                )
                db.expunge(account)
                return account, has_passkey
        token = _facade().request_token.get()
        if not token:
            raise _facade().AuthError(401, "未登录")
        claims = _facade()._decode_session_token(token)
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, claims.get("sub"))
            if account is None or not _facade().account_is_eligible(account):
                raise _facade().AuthError(401, "登录态无效")
            issued_at = _facade().datetime.fromtimestamp(
                float(claims.get("session_started_at", claims["iat"])), tz=_facade().UTC
            )
            revoked_at = account.sessions_revoked_at
            if (
                revoked_at
                and (revoked_at if revoked_at.tzinfo else revoked_at.replace(tzinfo=_facade().UTC)) >= issued_at
            ):
                raise _facade().AuthError(401, "登录态已失效")
            has_passkey = (
                db.query(_facade().Passkey.id).filter(_facade().Passkey.account_id == account.id).first() is not None
            )
            db.expunge(account)
            return account, has_passkey

    def current_user(self) -> CurrentUser:
        account, _ = self._authenticated_account()
        if _facade().is_local_superadmin(account):
            grants = _facade()._superadmin_grants()
            permissions = set(_facade().ALL_PERMISSIONS)
        elif not account.external_source:
            grants = _facade()._local_grants(account)
            permissions = {grant.code for grant in grants}
        else:
            grants = _facade()._snapshot_grants(account)
            permissions = {grant.code for grant in grants}
        return _facade().CurrentUser(
            id=str(account.id),
            account_id=str(account.id),
            is_local_superadmin=_facade().is_local_superadmin(account),
            name=account.username,
            email=account.email,
            avatar_url=account.avatar_url,
            ui_locale=account.ui_locale,
            must_change_password=account.must_change_password,
            has_local_password=bool(account.password_hash),
            permissions=sorted(permissions),
            grants=list(grants),
            role_groups=_facade()._snapshot_role_groups(account),
            security_capabilities=_facade().security_capabilities(account),
        )

    def revoke_sessions(self, account_id: str) -> None:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if account:
                account.sessions_revoked_at = _facade().datetime.now(_facade().UTC)
                db.commit()

    def change_password(self, account_id: str, current_password: str, new_password: str) -> bool:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if (
                account is None
                or not account.password_hash
                or not _facade().pwd_context.verify(current_password, account.password_hash)
            ):
                return False
            # 超管自助改密沿用 bootstrap 强度(≥12+弱值黑名单),防首登改密降级(06 §2)。
            if account.is_admin and _facade().is_unsafe_bootstrap_secret(new_password, min_length=12):
                raise _facade().AuthError(422, "管理员密码不符合强度要求")
            account.password_hash = _facade().pwd_context.hash(new_password)
            account.must_change_password = False
            account.sessions_revoked_at = _facade().datetime.now(_facade().UTC)
            db.commit()
            return True

    def totp_status(self, account_id: str) -> bool:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            return bool(account and account.totp_enabled)

    def totp_begin(self, account_id: str) -> tuple[str, str]:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if account is None or account.totp_enabled:
                raise _facade().AuthError(409, "TOTP 已启用, 请先禁用")
            secret = _facade().pyotp.random_base32()
            account.totp_pending_secret = secret
            db.commit()
            return secret, _facade().pyotp.TOTP(secret).provisioning_uri(
                account.username, issuer_name="Enterprise Platform"
            )

    def totp_confirm(self, account_id: str, code: str) -> bool:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if (
                account is None
                or not account.totp_pending_secret
                or not _facade().pyotp.TOTP(account.totp_pending_secret).verify(code)
            ):
                return False
            account.totp_secret = account.totp_pending_secret
            account.totp_pending_secret = None
            account.totp_enabled = True
            account.sessions_revoked_at = _facade().datetime.now(_facade().UTC)
            db.commit()
            return True

    def totp_disable(self, account_id: str, password: str, code: str) -> bool:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if (
                account is None
                or not account.totp_secret
                or not _facade().pwd_context.verify(password, account.password_hash)
                or not _facade().pyotp.TOTP(account.totp_secret).verify(code)
            ):
                return False
            account.totp_enabled = False
            account.totp_secret = None
            account.totp_pending_secret = None
            account.sessions_revoked_at = _facade().datetime.now(_facade().UTC)
            db.commit()
            return True

    def list_passkeys(self, account_id: str) -> list[PasskeySummary]:
        with _facade().SessionLocal() as db:
            rows = (
                db.query(_facade().Passkey)
                .filter(_facade().Passkey.account_id == account_id)
                .order_by(_facade().Passkey.created_at)
                .all()
            )
            return [_facade().PasskeySummary.model_validate(row) for row in rows]

    def begin_passkey_login(self, account_id: str) -> PasskeyChallenge:
        with _facade().SessionLocal() as db:
            rows = db.query(_facade().Passkey).filter(_facade().Passkey.account_id == account_id).all()
            config = _facade()._passkey_config()
            try:
                options, issued = _facade().shared_passkeys.begin_authentication(
                    user_id=account_id,
                    credential_ids=[row.credential_id for row in rows],
                    config=config,
                )
            except _facade().shared_passkeys.PasskeyError as exc:
                raise _facade().AuthError(exc.status_code, exc.detail) from exc
            _facade()._persist_passkey_challenge(
                db,
                account_id=account_id,
                purpose=config.authenticate_purpose,
                issued=issued,
            )
        return _facade().PasskeyChallenge(options=options, state_token=issued.token)

    def complete_passkey_login(self, account_id: str, state_token: str, credential: dict[str, Any]) -> bool:
        try:
            credential_id = _facade().shared_passkeys.credential_id(credential)
        except _facade().shared_passkeys.PasskeyError as exc:
            raise _facade().AuthError(exc.status_code, exc.detail) from exc
        config = _facade()._passkey_config()
        now = _facade().datetime.now(_facade().UTC)
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if account is None or not _facade().account_is_eligible(account, now=now):
                return False
            row = (
                db.query(_facade().Passkey)
                .filter(_facade().Passkey.account_id == account_id, _facade().Passkey.credential_id == credential_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                return False
            try:
                row.sign_count = _facade().shared_passkeys.complete_authentication(
                    user_id=account_id,
                    state_token=state_token,
                    credential=credential,
                    public_key=row.public_key,
                    current_sign_count=row.sign_count,
                    config=config,
                    challenge_consumer=lambda jti: _facade()._consume_passkey_challenge(
                        db,
                        account_id=account_id,
                        purpose=config.authenticate_purpose,
                        jti=jti,
                        now=now,
                    ),
                )
            except _facade().shared_passkeys.PasskeyError as exc:
                db.rollback()
                if exc.status_code in {400, 401} and "挑战" in str(exc.detail):
                    return False
                raise _facade().AuthError(exc.status_code, exc.detail) from exc
            row.last_used_at = now
            db.commit()
            return True

    def begin_passkey_registration(self, account_id: str) -> PasskeyChallenge:
        with _facade().SessionLocal() as db:
            account = db.get(_facade().Account, account_id)
            if account is None:
                raise _facade().AuthError(401, "登录态无效")
            config = _facade()._passkey_config()
            options, issued = _facade().shared_passkeys.begin_registration(
                user_id=account_id,
                username=account.username,
                config=config,
            )
            _facade()._persist_passkey_challenge(
                db,
                account_id=account_id,
                purpose=config.register_purpose,
                issued=issued,
            )
        return _facade().PasskeyChallenge(options=options, state_token=issued.token)

    def complete_passkey_registration(
        self, account_id: str, state_token: str, credential: dict[str, Any], name: str
    ) -> PasskeySummary:
        config = _facade()._passkey_config()
        now = _facade().datetime.now(_facade().UTC)
        with _facade().SessionLocal() as db:
            try:
                result = _facade().shared_passkeys.complete_registration(
                    user_id=account_id,
                    state_token=state_token,
                    credential=credential,
                    config=config,
                    challenge_consumer=lambda jti: _facade()._consume_passkey_challenge(
                        db,
                        account_id=account_id,
                        purpose=config.register_purpose,
                        jti=jti,
                        now=now,
                    ),
                )
            except _facade().shared_passkeys.PasskeyError as exc:
                db.rollback()
                raise _facade().AuthError(exc.status_code, exc.detail) from exc
            if (
                db.query(_facade().Passkey.id).filter(_facade().Passkey.credential_id == result.credential_id).first()
                is not None
            ):
                db.rollback()
                raise _facade().AuthError(409, "该通行密钥已注册")
            row = _facade().Passkey(
                account_id=account_id,
                credential_id=result.credential_id,
                public_key=result.public_key,
                sign_count=result.sign_count,
                name=name,
            )
            db.add(row)
            account = db.get(_facade().Account, account_id)
            if account is not None:
                account.sessions_revoked_at = now
            try:
                db.commit()
            except _facade().IntegrityError as exc:
                db.rollback()
                raise _facade().AuthError(409, "该通行密钥已注册") from exc
            db.refresh(row)
            return _facade().PasskeySummary.model_validate(row)

    def delete_passkey(self, account_id: str, passkey_id: str) -> bool:
        try:
            parsed_passkey_id = _facade().uuid.UUID(passkey_id)
        except ValueError:
            return False
        with _facade().SessionLocal() as db:
            row = (
                db.query(_facade().Passkey)
                .filter(_facade().Passkey.account_id == account_id, _facade().Passkey.id == parsed_passkey_id)
                .one_or_none()
            )
            if row is None:
                return False
            db.delete(row)
            account = db.get(_facade().Account, account_id)
            if account is not None:
                account.sessions_revoked_at = _facade().datetime.now(_facade().UTC)
            db.commit()
            return True


class BlankLocalAccountUnitOfWork:
    """SQLAlchemy 事务适配；退出上下文时业务写入与审计只提交一次。"""

    def __init__(self) -> None:
        self.db = _facade().SessionLocal()

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
        return _facade().LocalAccountRecord(
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
            self.db.query(_facade().Passkey.account_id, _facade().func.count(_facade().Passkey.id))
            .group_by(_facade().Passkey.account_id)
            .subquery()
        )
        query = (
            self.db.query(_facade().Account, _facade().func.coalesce(passkey_counts.c.count, 0))
            .outerjoin(passkey_counts, passkey_counts.c.account_id == _facade().Account.id)
            .filter(_facade().Account.external_source.is_(None))
        )
        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.filter(
                _facade().or_(_facade().Account.username.ilike(term), _facade().Account.email.ilike(term))
            )
        rows = query.order_by(_facade().Account.created_at.asc(), _facade().Account.username.asc()).all()
        return [self._record(account, passkey_count=int(count or 0)) for account, count in rows]

    def get_account(self, account_id: uuid.UUID, *, for_update: bool = False) -> LocalAccountRecord | None:
        query = self.db.query(_facade().Account).filter(
            _facade().Account.id == account_id,
            _facade().Account.external_source.is_(None),
        )
        if for_update:
            query = query.with_for_update()
        account = query.one_or_none()
        if account is None:
            return None
        passkey_count = int(
            self.db.query(_facade().func.count(_facade().Passkey.id))
            .filter(_facade().Passkey.account_id == account.id)
            .scalar()
            or 0
        )
        return self._record(account, passkey_count=passkey_count)

    def permission_catalog(self) -> list[LocalPermissionRecord]:
        rows = self.db.query(_facade().PermissionCatalog).order_by(_facade().PermissionCatalog.code.asc()).all()
        return [
            _facade().LocalPermissionRecord(
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
        self.db.query(_facade().Account.id).filter(
            _facade().Account.external_source.is_(None),
            _facade().Account.is_admin.is_(True),
        ).order_by(_facade().Account.id).with_for_update().all()

    def usable_local_admin_count(self, *, now: datetime) -> int:
        self.db.flush()
        return int(
            self.db.query(_facade().func.count(_facade().Account.id))
            .filter(
                _facade().Account.external_source.is_(None),
                _facade().Account.is_admin.is_(True),
                _facade().Account.active.is_(True),
                _facade().or_(_facade().Account.expires_at.is_(None), _facade().Account.expires_at > now),
            )
            .scalar()
            or 0
        )

    def create_account(self, **values: Any) -> LocalAccountRecord:
        password = values.pop("password")
        account = _facade().Account(password_hash=_facade().pwd_context.hash(password), **values)
        self.db.add(account)
        try:
            self.db.flush()
        except _facade().IntegrityError as exc:
            raise _facade().AuthError(409, "用户名已被占用") from exc
        return self._record(account)

    def update_account(self, account_id: uuid.UUID, **values: Any) -> LocalAccountRecord:
        account = self.db.get(_facade().Account, account_id)
        if account is None:
            raise _facade().AuthError(404, "本地账户不存在")
        password = values.pop("password", None)
        if password is not None:
            account.password_hash = _facade().pwd_context.hash(password)
        for key, value in values.items():
            setattr(account, key, value)
        self.db.flush()
        passkey_count = int(
            self.db.query(_facade().func.count(_facade().Passkey.id))
            .filter(_facade().Passkey.account_id == account.id)
            .scalar()
            or 0
        )
        return self._record(account, passkey_count=passkey_count)

    def delete_account(self, account_id: uuid.UUID) -> None:
        account = self.db.get(_facade().Account, account_id)
        if account is None:
            raise _facade().AuthError(404, "本地账户不存在")
        self.db.query(_facade().Passkey).filter(_facade().Passkey.account_id == account.id).delete()
        self.db.query(_facade().Notification).filter(_facade().Notification.account_id == account.id).delete()
        self.db.query(_facade().PermissionSnapshot).filter(
            _facade().PermissionSnapshot.account_id == account.id
        ).delete()
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
            _facade().PlatformAuditLog(
                actor_id=str(actor_id).strip()[:100],
                action=str(action).strip()[:160],
                before_data=_facade().jsonable_encoder(_facade()._redacted_setting(before or {}))
                if before is not None
                else None,
                after_data=_facade().jsonable_encoder(_facade()._redacted_setting(after or {}))
                if after is not None
                else None,
            )
        )
        self.db.flush()


class BlankLocalAccountAdmin(LocalAccountAdminPort):
    def unit_of_work(self) -> BlankLocalAccountUnitOfWork:
        return _facade().BlankLocalAccountUnitOfWork()


account_adapter = BlankAccountAdapter()
local_account_admin = BlankLocalAccountAdmin()


def require_permission(code: str) -> None:
    user = _facade().account_adapter.current_user()
    if code not in user.permissions:
        raise _facade().AuthError(403, "缺少权限")


def _catalog_permissions(db) -> dict[str, CatalogPermission]:
    """目录坏 scope 逐项忽略，不能让一条脏配置扩大权限或毒化整次读取。"""

    catalog: dict[str, CatalogPermission] = {}
    for row in db.query(_facade().PermissionCatalog).all():
        scopes: set[DataScope] = set()
        for raw_scope in row.supported_scopes if isinstance(row.supported_scopes, list | tuple) else ():
            try:
                scopes.add(_facade().DataScope(str(raw_scope)))
            except ValueError:
                continue
        catalog[row.code] = _facade().CatalogPermission(
            code=row.code,
            supported_scopes=frozenset(scopes),
            active=row.active,
        )
    return catalog


def _snapshot_grants(account: Account) -> tuple[NormalizedGrant, ...]:
    return _facade().snapshot_grants_for_account(account)


def _local_grants(account: Account) -> tuple[NormalizedGrant, ...]:
    with _facade().SessionLocal() as db:
        catalog = _facade()._catalog_permissions(db)
    baseline = ({"code": code, "scope": "SELF"} for code in _facade().BASELINE_SELF_SERVICE)
    stored = account.local_permissions if isinstance(account.local_permissions, list) else []
    return _facade().normalize_local_grants((*baseline, *stored), catalog)


def _superadmin_grants() -> tuple[NormalizedGrant, ...]:
    """超管权限来自单一注册表；ALL 显式支配 SELF，不作 scope 序数比较。"""

    grants: list[NormalizedGrant] = []
    for permission in _facade().FRAMEWORK_PERMISSIONS:
        scopes = set(permission.supported_scopes) & {_facade().DataScope.SELF, _facade().DataScope.ALL}
        if _facade().DataScope.ALL in scopes:
            scope = _facade().DataScope.ALL
        elif _facade().DataScope.SELF in scopes:
            scope = _facade().DataScope.SELF
        else:
            continue
        grants.append(_facade().NormalizedGrant(code=permission.code, scope=scope))
    return tuple(grants)


def _snapshot_role_groups(account: Account) -> list[str]:
    if not account.external_source or not account.external_user_id:
        return []
    with _facade().SessionLocal() as db:
        integration = db.get(_facade().PlatformSetting, "easyauth")
        app_key = str((integration.value if integration else {}).get("app_key") or "").strip()
        if not app_key:
            return []
        snapshot = (
            db.query(_facade().PermissionSnapshot.groups)
            .filter(
                _facade().PermissionSnapshot.account_id == account.id,
                _facade().PermissionSnapshot.app_key == app_key,
                _facade().PermissionSnapshot.expires_at > _facade().datetime.now(_facade().UTC),
            )
            .order_by(_facade().PermissionSnapshot.fetched_at.desc())
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
