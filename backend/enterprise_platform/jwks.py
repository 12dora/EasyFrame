"""Authentik/JWKS 严格健康探测。"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request
from enterprise_platform.schemas import ConnectionTestResult


def probe_jwks(
    url: str,
    *,
    timeout_seconds: float = 5,
    allow_localhost: bool = False,
    transport: Any = guarded_request,
) -> ConnectionTestResult:
    """仅 HTTP 200 且含合法、非空 keys 的 JWKS 才健康。"""

    if not url.strip():
        return ConnectionTestResult(
            ok=False, error_kind="not_configured", error_detail="jwks endpoint is not configured"
        )
    started = datetime.now(UTC)
    try:
        response = transport(
            "GET",
            url,
            headers={"User-Agent": "enterprise-platform-health"},
            timeout=timeout_seconds,
            allow_localhost=allow_localhost,
            max_body_bytes=256 * 1024,
        )
    except UnsafeOutboundUrlError as exc:
        return ConnectionTestResult(ok=False, error_kind="blocked", error_detail=str(exc)[:300])
    except (OSError, httpx.HTTPError) as exc:
        return ConnectionTestResult(ok=False, error_kind="unreachable", error_detail=str(exc)[:300])
    latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if response.status_code != 200:
        return ConnectionTestResult(
            ok=False,
            latency_ms=latency,
            error_kind="http_error",
            error_detail=f"jwks returned HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError:
        return ConnectionTestResult(
            ok=False, latency_ms=latency, error_kind="invalid_response", error_detail="jwks response is not JSON"
        )
    if not valid_jwks(payload):
        return ConnectionTestResult(
            ok=False,
            latency_ms=latency,
            error_kind="invalid_response",
            error_detail="jwks response must contain non-empty valid keys",
        )
    return ConnectionTestResult(ok=True, latency_ms=latency)


def valid_jwks(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 50:
        return False
    seen: set[str] = set()
    for key in keys:
        kid = _valid_key_id(key, seen)
        if kid is None or not _valid_key_material(key):
            return False
        seen.add(kid)
    return True


def _valid_key_id(key: Any, seen: set[str]) -> str | None:
    """合法且未重复的 kid 才返回,否则 None(即拒绝该 JWKS)。"""

    if not isinstance(key, dict):
        return None
    kid = key.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 256 or kid in seen:
        return None
    return kid


def _valid_key_material(key: dict[str, Any]) -> bool:
    if key.get("use") not in {None, "sig"}:
        return False
    kty = key.get("kty")
    if kty == "RSA":
        return key.get("alg") in {None, "RS256"} and _valid_rsa_key(key)
    if kty == "EC":
        return key.get("alg") in {None, "ES256"} and _valid_ec_key(key)
    return False


def _valid_rsa_key(key: dict[str, Any]) -> bool:
    modulus = _base64url_uint(key.get("n"))
    exponent = _base64url_uint(key.get("e"))
    return modulus is not None and modulus.bit_length() >= 2048 and exponent in {3, 65537}


def _valid_ec_key(key: dict[str, Any]) -> bool:
    if key.get("crv") != "P-256":
        return False
    x = _base64url_bytes(key.get("x"))
    y = _base64url_bytes(key.get("y"))
    return x is not None and y is not None and len(x) == 32 and len(y) == 32


def _base64url_uint(value: Any) -> int | None:
    decoded = _base64url_bytes(value)
    if not decoded:
        return None
    return int.from_bytes(decoded, "big")


def _base64url_bytes(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > 4096 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        return None
