"""可信上游 principal 的宿主无关验签与 claims 校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jose import JWTError, jwt

DEFAULT_MAX_PRINCIPAL_LIFETIME_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 30


class PrincipalValidationError(Exception):
    def __init__(self, detail: str, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class UpstreamPrincipal:
    sub: str
    issuer: str
    audience: str
    active: bool
    name: str
    email: str
    external_source: str = "authentik"
    avatar_url: str | None = None
    ui_locale: str | None = None

    @classmethod
    def from_claims(
        cls,
        claims: dict[str, Any],
        issuer: str,
        audience: str,
        *,
        max_lifetime_seconds: int = DEFAULT_MAX_PRINCIPAL_LIFETIME_SECONDS,
    ) -> UpstreamPrincipal:
        for key in ("sub", "iss", "name", "email"):
            if not isinstance(claims.get(key), str) or not claims[key].strip():
                raise PrincipalValidationError(f"missing {key}")
        for key in ("aud", "exp", "iat", "active"):
            if claims.get(key) is None:
                raise PrincipalValidationError(f"missing {key}")
        if claims["iss"] != issuer:
            raise PrincipalValidationError("principal issuer mismatch")
        actual_audience = claims["aud"]
        if not (actual_audience == audience or isinstance(actual_audience, list) and audience in actual_audience):
            raise PrincipalValidationError("principal audience mismatch")
        if claims["active"] is not True:
            raise PrincipalValidationError("inactive principal")
        now = datetime.now(UTC).timestamp()
        expires_at = _timestamp(claims["exp"], "exp")
        issued_at = _timestamp(claims["iat"], "iat")
        if expires_at <= now:
            raise PrincipalValidationError("principal expired")
        if issued_at > now + MAX_CLOCK_SKEW_SECONDS:
            raise PrincipalValidationError("principal iat is in the future")
        if expires_at <= issued_at or expires_at - issued_at > max_lifetime_seconds:
            raise PrincipalValidationError("principal lifetime exceeds maximum")
        if now - issued_at > max_lifetime_seconds + MAX_CLOCK_SKEW_SECONDS:
            raise PrincipalValidationError("principal is too old")
        return cls(
            sub=claims["sub"],
            issuer=claims["iss"],
            audience=audience,
            active=True,
            name=claims["name"],
            email=claims["email"],
            avatar_url=_optional_text(claims.get("avatar_url")),
            ui_locale=_optional_text(claims.get("ui_locale")),
        )


def parse_upstream_principal_from_headers(
    headers: dict[str, str],
    *,
    mode: str,
    header_name: str,
    issuer: str | None,
    audience: str | None,
    secret: str | None,
    max_lifetime_seconds: int = DEFAULT_MAX_PRINCIPAL_LIFETIME_SECONDS,
) -> UpstreamPrincipal | None:
    if mode == "disabled":
        return None
    token = next((value for key, value in headers.items() if key.lower() == header_name.lower()), None)
    if not token:
        return None
    if not issuer or not audience or not secret:
        raise PrincipalValidationError("principal verifier is not configured")
    if mode == "jwt":
        try:
            claims = jwt.decode(token, secret, algorithms=["HS256"], audience=audience, issuer=issuer)
        except JWTError as exc:
            raise PrincipalValidationError("invalid principal jwt") from exc
    elif mode == "header":
        try:
            payload, signature = token.split(".", 1)
            expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_decode(signature), expected):
                raise PrincipalValidationError("invalid principal header signature")
            claims = json.loads(_decode(payload))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PrincipalValidationError("invalid principal header envelope") from exc
    else:
        raise PrincipalValidationError("unsupported principal mode")
    if not isinstance(claims, dict):
        raise PrincipalValidationError("invalid principal claims")
    return UpstreamPrincipal.from_claims(
        claims,
        issuer,
        audience,
        max_lifetime_seconds=max_lifetime_seconds,
    )


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode())


def _timestamp(value: Any, key: str) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, int | float):
        return float(value)
    raise PrincipalValidationError(f"invalid {key}")


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
