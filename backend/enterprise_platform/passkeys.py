"""宿主无关 WebAuthn/Passkey 挑战与验证核心。"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import webauthn
from jose import JWTError, jwt
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

STATE_TTL_SECONDS = 300
REGISTER_PURPOSE = "enterprise-passkey-register"
AUTHENTICATE_PURPOSE = "enterprise-passkey-authenticate"

# 一次性挑战消费者:入参 jti,返回 True 表示本调用赢得消费。
ChallengeConsumer = Callable[[str], bool]


@dataclass(frozen=True)
class PasskeyConfig:
    rp_id: str
    rp_name: str
    origins: tuple[str, ...]
    signing_secret: str
    register_purpose: str = REGISTER_PURPOSE
    authenticate_purpose: str = AUTHENTICATE_PURPOSE


@dataclass(frozen=True)
class RegistrationResult:
    credential_id: str
    public_key: str
    sign_count: int
    transports: list[str]


@dataclass(frozen=True)
class IssuedState:
    """签发的一次性挑战状态;宿主应持久化 ``jti`` 并在完成时原子消费。"""

    token: str
    jti: str
    challenge: bytes
    expires_at: int


@dataclass(frozen=True)
class VerifiedState:
    """验签后的挑战状态(含 JTI,供宿主一次消费)。"""

    challenge: bytes
    jti: str
    purpose: str
    user_id: str
    expires_at: int


class PasskeyError(Exception):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def begin_registration(*, user_id: str, username: str, config: PasskeyConfig) -> tuple[dict[str, Any], IssuedState]:
    """签发注册挑战及宿主必须持久化的一次性状态。"""

    options = webauthn.generate_registration_options(
        rp_id=config.rp_id,
        rp_name=config.rp_name,
        user_id=user_id.encode(),
        user_name=username,
        user_display_name=username,
    )
    issued = issue_state_full(
        purpose=config.register_purpose,
        user_id=user_id,
        challenge=options.challenge,
        config=config,
    )
    return json.loads(options_to_json(options)), issued


def complete_registration(
    *,
    user_id: str,
    state_token: str,
    credential: dict[str, Any],
    config: PasskeyConfig,
    challenge_consumer: ChallengeConsumer,
) -> RegistrationResult:
    """完成注册断言。

    ``challenge_consumer`` **必填**:共享层 fail-closed,不传或返回 False 不得成功。
    宿主须在同一事务/原子边界内实现一次消费(例如条件 UPDATE consumed_at)。
    """

    _require_challenge_consumer(challenge_consumer)
    verified = verify_state_claims(state_token, purpose=config.register_purpose, user_id=user_id, config=config)
    _consume_challenge(challenge_consumer, verified.jti)
    credential_json = json.dumps(credential)
    try:
        verification = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=verified.challenge,
            expected_rp_id=config.rp_id,
            expected_origin=list(config.origins),
        )
    except Exception as exc:
        raise PasskeyError(f"Passkey 注册校验失败: {exc}") from exc
    return RegistrationResult(
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=base64.b64encode(verification.credential_public_key).decode(),
        sign_count=verification.sign_count,
        transports=credential_transports(credential),
    )


def begin_authentication(
    *, user_id: str, credential_ids: list[str], config: PasskeyConfig
) -> tuple[dict[str, Any], IssuedState]:
    """签发认证挑战及宿主必须持久化的一次性状态。"""

    if not credential_ids:
        raise PasskeyError("该用户未注册通行密钥")
    options = webauthn.generate_authentication_options(
        rp_id=config.rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(item)) for item in credential_ids],
    )
    issued = issue_state_full(
        purpose=config.authenticate_purpose,
        user_id=user_id,
        challenge=options.challenge,
        config=config,
    )
    return json.loads(options_to_json(options)), issued


def complete_authentication(
    *,
    user_id: str,
    state_token: str,
    credential: dict[str, Any],
    public_key: str,
    current_sign_count: int,
    config: PasskeyConfig,
    challenge_consumer: ChallengeConsumer,
) -> int:
    """完成登录断言并返回新 sign_count。

    ``challenge_consumer`` **必填**:共享验证路径在 WebAuthn 校验前原子消费 jti;
    未提供消费者 → 显式错误(永不静默放行)。零 counter 认证器依赖此一次消费防重放。
    """

    _require_challenge_consumer(challenge_consumer)
    verified = verify_state_claims(state_token, purpose=config.authenticate_purpose, user_id=user_id, config=config)
    _consume_challenge(challenge_consumer, verified.jti)
    try:
        verification = webauthn.verify_authentication_response(
            credential=json.dumps(credential),
            expected_challenge=verified.challenge,
            expected_rp_id=config.rp_id,
            expected_origin=list(config.origins),
            credential_public_key=base64.b64decode(public_key),
            credential_current_sign_count=current_sign_count,
        )
    except Exception as exc:
        raise PasskeyError("Passkey 验证失败", status_code=401) from exc
    return verification.new_sign_count


def credential_id(credential: dict[str, Any]) -> str:
    value = credential.get("rawId") or credential.get("id")
    if not isinstance(value, str) or not value:
        raise PasskeyError("Passkey 凭据缺少 id")
    return value


def credential_transports(credential: dict[str, Any]) -> list[str]:
    response = credential.get("response")
    transports = response.get("transports") if isinstance(response, dict) else None
    return [item for item in transports if isinstance(item, str)] if isinstance(transports, list) else []


def hash_jti(jti: str) -> str:
    """固定宽度挑战键:存库与条件更新使用哈希,不落明文 JTI。"""

    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _require_challenge_consumer(challenge_consumer: ChallengeConsumer | None) -> None:
    if challenge_consumer is None or not callable(challenge_consumer):
        raise PasskeyError(
            "Passkey challenge_consumer is required; refusing silent pass without one-time consume",
            status_code=500,
        )


def _consume_challenge(challenge_consumer: ChallengeConsumer, jti: str) -> None:
    try:
        ok = bool(challenge_consumer(jti))
    except PasskeyError:
        raise
    except Exception as exc:  # 宿主消费失败统一 fail-closed
        raise PasskeyError("Passkey 挑战消费失败, 请重试", status_code=401) from exc
    if not ok:
        raise PasskeyError("Passkey 挑战无效或已使用, 请重试", status_code=401)


class ProcessChallengeLedger:
    """进程内原子一次性挑战账本(单进程宿主 / 测试用)。

    多 worker 生产宿主应改用 DB 条件更新(见 customs PasskeyCeremony)。
    register 在 begin 路径调用;consume 作为 challenge_consumer 传入 complete_*。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, int] = {}  # jti_hash -> expires_at epoch
        self._consumed: set[str] = set()

    def register(self, jti: str, expires_at: int) -> None:
        key = hash_jti(jti)
        with self._lock:
            self._pending[key] = int(expires_at)
            self._consumed.discard(key)

    def consume(self, jti: str) -> bool:
        key = hash_jti(jti)
        now = int(time.time())
        with self._lock:
            exp = self._pending.pop(key, None)
            if exp is None:
                return False
            if now > exp:
                return False
            if key in self._consumed:
                return False
            self._consumed.add(key)
            # 有界清理:防止 consumed 无限增长
            if len(self._consumed) > 10_000:
                self._consumed.clear()
                self._consumed.add(key)
            return True


def issue_state_full(*, purpose: str, user_id: str, challenge: bytes, config: PasskeyConfig) -> IssuedState:
    """签发带 JTI 的挑战状态(完整对象)。新宿主请用本函数并持久化 ``jti``。"""

    now = int(time.time())
    expires_at = now + STATE_TTL_SECONDS
    jti = secrets.token_urlsafe(32)
    token = jwt.encode(
        {
            "purpose": purpose,
            "challenge": bytes_to_base64url(challenge),
            "user_id": user_id,
            "jti": jti,
            "iat": now,
            "exp": expires_at,
        },
        config.signing_secret,
        algorithm="HS256",
    )
    return IssuedState(token=token, jti=jti, challenge=challenge, expires_at=expires_at)


def issue_state(*, purpose: str, user_id: str, challenge: bytes, config: PasskeyConfig) -> str:
    """向后兼容:返回 JWT 字符串。新代码请用 ``issue_state_full``。"""

    return issue_state_full(purpose=purpose, user_id=user_id, challenge=challenge, config=config).token


def verify_state_claims(state_token: str, *, purpose: str, user_id: str, config: PasskeyConfig) -> VerifiedState:
    """验签挑战 JWT,返回 challenge + jti。

    仅验签,不消费。完成路径必须经 ``complete_*`` 并提供 challenge_consumer。
    """

    try:
        claims = jwt.decode(state_token, config.signing_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise PasskeyError("Passkey 挑战无效或已过期, 请重试") from exc
    if claims.get("purpose") != purpose or str(claims.get("user_id")) != user_id:
        raise PasskeyError("Passkey 挑战无效, 请重试")
    challenge = str(claims.get("challenge") or "")
    jti = str(claims.get("jti") or "")
    if not challenge or not jti:
        raise PasskeyError("Passkey 挑战无效, 请重试")
    exp = claims.get("exp")
    expires_at = int(exp) if exp is not None else int(time.time()) + STATE_TTL_SECONDS
    return VerifiedState(
        challenge=base64url_to_bytes(challenge),
        jti=jti,
        purpose=str(claims.get("purpose") or purpose),
        user_id=str(claims.get("user_id") or user_id),
        expires_at=expires_at,
    )


def verify_state(state_token: str, *, purpose: str, user_id: str, config: PasskeyConfig) -> bytes:
    """向后兼容:仅返回 challenge 字节。完成路径不得单独依赖本函数防重放。"""

    return verify_state_claims(state_token, purpose=purpose, user_id=user_id, config=config).challenge
