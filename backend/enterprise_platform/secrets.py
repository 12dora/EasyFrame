"""应用级 envelope encryption；密钥缺失或密文损坏时 fail-closed。"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class SecretConfigurationError(RuntimeError):
    pass


PREFIX = "enc:v1:"


def encrypt_secret(value: str, *, key_env: str = "BLANK_INTEGRATION_ENVELOPE_KEY") -> str:
    if not value:
        return ""
    return PREFIX + _fernet(key_env).encrypt(value.encode()).decode()


def decrypt_secret(value: str, *, key_env: str = "BLANK_INTEGRATION_ENVELOPE_KEY") -> str:
    if not value:
        return ""
    if not value.startswith(PREFIX):
        raise SecretConfigurationError("stored integration secret is not encrypted")
    try:
        return _fernet(key_env).decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretConfigurationError("stored integration secret cannot be decrypted") from exc


def _fernet(key_env: str) -> Fernet:
    raw = os.getenv(key_env, "").strip()
    if not raw:
        raise SecretConfigurationError(f"{key_env} is required when integration credentials are configured")
    try:
        return Fernet(raw.encode())
    except (TypeError, ValueError) as exc:
        raise SecretConfigurationError(f"{key_env} must be a valid Fernet key") from exc
