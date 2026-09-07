"""可复用 Authentik/OIDC 授权码 + PKCE 登录服务与 router factory。"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt

from enterprise_platform.jwks import valid_jwks
from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request

STATE_COOKIE_NAME = "enterprise_oidc_state"
STATE_COOKIE_PATH = "/api/v1/auth/oidc"
STATE_PURPOSE = "enterprise-oidc-state"
STATE_TTL_SECONDS = 600
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_PROVIDER_ERROR_KINDS = frozenset(
    {
        "access_denied",
        "consent_required",
        "interaction_required",
        "login_required",
        "server_error",
        "temporarily_unavailable",
    }
)


class OidcFlowError(Exception):
    def __init__(self, detail: str, *, kind: str = "flow_error", status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.kind = kind
        self.status_code = status_code


class OidcHttpTransport(Protocol):
    """OIDC 出站请求边界；宿主可在测试中注入无网络 transport。"""

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(frozen=True)
class OidcConfig:
    enabled: bool
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str
    client_id: str
    client_secret: str
    scopes: str
    redirect_uri: str
    frontend_base_url: str
    signing_secret: str
    timeout_seconds: float = 5
    allow_local_outbound: bool = False
    http_transport: OidcHttpTransport = guarded_request

    def validated(self) -> OidcConfig:
        if not self.enabled:
            raise OidcFlowError("Authentik 登录未启用", kind="not_configured", status_code=404)
        required = (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "client_id",
            "client_secret",
            "redirect_uri",
            "frontend_base_url",
            "signing_secret",
        )
        missing = [field for field in required if not str(getattr(self, field)).strip()]
        if missing:
            raise OidcFlowError(
                "Authentik 登录配置不完整: " + ", ".join(missing), kind="not_configured", status_code=409
            )
        return self


@dataclass(frozen=True)
class OidcIdentity:
    sub: str
    name: str
    email: str
    avatar_url: str | None
    title: str | None = None
    dingtalk_user_id: str | None = None

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> OidcIdentity:
        sub = _text(claims.get("sub"))
        name = _text(claims.get("name")) or _text(claims.get("preferred_username"))
        if not sub or not name:
            raise OidcFlowError("id_token 缺少 sub 或 name", kind="invalid_token", status_code=401)
        return cls(
            sub,
            name,
            _text(claims.get("email")),
            _text(claims.get("picture")) or None,
            _text(claims.get("dingtalk_title")) or None,
            _text(claims.get("dingtalk_user_id")) or None,
        )


class OidcHost(Protocol):
    def config(self) -> OidcConfig: ...
    def upsert_identity(self, identity: OidcIdentity) -> str: ...
    def issue_session(self, account_id: str) -> str: ...
    def revoke_sessions_by_subject(self, sub: str) -> int: ...


def default_locale_resolver(request: Request) -> str | None:
    """从显式 query/header/next-intl cookie 解析登录发起页 locale。"""

    return (
        request.query_params.get("locale") or request.headers.get("X-UI-Locale") or request.cookies.get("NEXT_LOCALE")
    )


@dataclass(frozen=True)
class OidcRouteConfig:
    api_base_path: str = "/api/v1"
    frontend_complete_path: str = "/login/oidc-complete"
    frontend_login_path: str = "/login"
    frontend_silent_path: str = "/login/oidc-silent"
    supported_locales: tuple[str, ...] = ("zh-CN", "en")
    default_locale: str = "zh-CN"
    force_secure_cookies: bool = False
    state_cookie_name: str = STATE_COOKIE_NAME
    locale_resolver: Callable[[Request], str | None] = default_locale_resolver

    @property
    def cookie_path(self) -> str:
        return f"{self.api_base_path.rstrip('/')}/auth/oidc"

    @property
    def authorize_path(self) -> str:
        return f"{self.api_base_path.rstrip('/')}/auth/oidc/authorize"


def sanitize_next(next_path: str | None) -> str | None:
    if (
        not next_path
        or not next_path.startswith("/")
        or next_path.startswith("//")
        or "\\" in next_path
        or any(ord(char) < 32 for char in next_path)
    ):
        return None
    return next_path


def issue_state(
    next_path: str | None, config: OidcConfig, *, locale: str | None = None, silent: bool = False
) -> tuple[str, str, str, str]:
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = int(time.time())
    cookie = jwt.encode(
        {
            "purpose": STATE_PURPOSE,
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "next": sanitize_next(next_path) or "",
            "locale": locale or "",
            "silent": silent,
            "iat": now,
            "exp": now + STATE_TTL_SECONDS,
        },
        config.signing_secret,
        algorithm="HS256",
    )
    return state, nonce, challenge, cookie


def _state_cookie_claims(cookie: str | None, config: OidcConfig, *, verify_exp: bool = True) -> dict[str, Any]:
    if not cookie:
        raise OidcFlowError("登录状态缺失或已过期,请重新登录", kind="state_mismatch")
    try:
        claims = jwt.decode(cookie, config.signing_secret, algorithms=["HS256"], options={"verify_exp": verify_exp})
    except JWTError as exc:
        raise OidcFlowError("登录状态无效或已过期,请重新登录", kind="state_mismatch") from exc
    if claims.get("purpose") != STATE_PURPOSE:
        raise OidcFlowError("登录状态不匹配,请重新登录", kind="state_mismatch")
    return claims


def verify_state(cookie: str | None, state: str | None, config: OidcConfig) -> dict[str, Any]:
    claims = _state_cookie_claims(cookie, config)
    if not state or not secrets.compare_digest(str(claims.get("state")), state):
        raise OidcFlowError("登录状态不匹配,请重新登录", kind="state_mismatch")
    return claims


def build_authorize_url(config: OidcConfig, *, state: str, nonce: str, challenge: str, silent: bool = False) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scopes or "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **({"prompt": "none"} if silent else {}),
        }
    )
    return f"{config.authorization_endpoint}{'&' if '?' in config.authorization_endpoint else '?'}{query}"


def exchange_code(config: OidcConfig, code: str, verifier: str) -> dict[str, Any]:
    try:
        response = config.http_transport(
            "POST",
            config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "code_verifier": verifier,
            },
            auth=(config.client_id, config.client_secret),
            timeout=config.timeout_seconds,
            allow_localhost=config.allow_local_outbound,
        )
    except (httpx.HTTPError, UnsafeOutboundUrlError) as exc:
        raise OidcFlowError("无法访问 Authentik token 端点", kind="unreachable", status_code=502) from exc
    if response.status_code != 200:
        raise OidcFlowError(f"Authentik token 端点返回 {response.status_code}", kind="token_error", status_code=502)
    try:
        payload = response.json()
    except ValueError as exc:
        raise OidcFlowError("Authentik token 响应不是 JSON", kind="invalid_response", status_code=502) from exc
    if not isinstance(payload, dict) or not payload.get("id_token"):
        raise OidcFlowError("Authentik token 响应缺少 id_token", kind="invalid_response", status_code=502)
    return payload


def fetch_jwks(config: OidcConfig, force: bool = False) -> dict[str, Any]:
    cached = _jwks_cache.get(config.jwks_uri)
    if cached and not force and cached[0] > time.time():
        return cached[1]
    try:
        response = config.http_transport(
            "GET",
            config.jwks_uri,
            timeout=config.timeout_seconds,
            allow_localhost=config.allow_local_outbound,
            max_body_bytes=256 * 1024,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, UnsafeOutboundUrlError, ValueError) as exc:
        raise OidcFlowError("无法获取 Authentik JWKS", kind="unreachable", status_code=502) from exc
    if not valid_jwks(payload):
        raise OidcFlowError("Authentik JWKS 响应格式无效", kind="invalid_response", status_code=502)
    _jwks_cache[config.jwks_uri] = (time.time() + 600, payload)
    return payload


def _matching_jwks_key(payload: dict[str, Any], kid: Any) -> dict[str, Any] | None:
    keys = [item for item in payload.get("keys", []) if isinstance(item, dict)]
    return next((item for item in keys if kid is None or item.get("kid") == kid), None)


def _signing_key(config: OidcConfig, kid: Any) -> dict[str, Any]:
    """先用缓存 JWKS 匹配;未命中再强制刷新一次,仍未命中即拒绝。"""

    key = _matching_jwks_key(fetch_jwks(config), kid)
    if key is None:
        key = _matching_jwks_key(fetch_jwks(config, True), kid)
    if key is None:
        raise OidcFlowError("id_token 签名密钥不在 JWKS 中", kind="invalid_token", status_code=401)
    return key


def _decoded_id_token(config: OidcConfig, token: str, key: dict[str, Any], algorithm: Any) -> dict[str, Any]:
    try:
        if key.get("use") not in {None, "sig"} or key.get("alg") not in {None, algorithm}:
            raise OidcFlowError("id_token 签名密钥用途不匹配", kind="invalid_token", status_code=401)
        return jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=config.client_id,
            issuer=config.issuer,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        raise OidcFlowError("id_token 校验失败", kind="invalid_token", status_code=401) from exc


def validate_id_token(config: OidcConfig, token: str, nonce: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise OidcFlowError("id_token header 无效", kind="invalid_token", status_code=401) from exc
    kid = header.get("kid")
    algorithm = header.get("alg")
    if algorithm not in {"RS256", "ES256"}:
        raise OidcFlowError("id_token 算法不受支持", kind="invalid_token", status_code=401)
    claims = _decoded_id_token(config, token, _signing_key(config, kid), algorithm)
    if nonce and claims.get("nonce") != nonce:
        raise OidcFlowError("id_token nonce 不匹配", kind="invalid_token", status_code=401)
    return claims


def fetch_userinfo(config: OidcConfig, token: str) -> dict[str, Any]:
    if not config.userinfo_endpoint or not token:
        return {}
    try:
        response = config.http_transport(
            "GET",
            config.userinfo_endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.timeout_seconds,
            allow_localhost=config.allow_local_outbound,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, UnsafeOutboundUrlError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def create_oidc_router(host: OidcHost, *, routes: OidcRouteConfig | None = None) -> APIRouter:
    from enterprise_platform.oidc_callback import register_oidc_callback
    from enterprise_platform.oidc_logout import register_backchannel_logout
    from enterprise_platform.oidc_start import register_oidc_start

    route_config = routes or OidcRouteConfig()
    router = APIRouter(prefix="/auth/oidc", tags=["auth-oidc"])

    register_oidc_start(router, host, route_config)

    register_oidc_callback(router, host, route_config)
    register_backchannel_logout(router, host)
    return router


def _error_redirect(
    config: OidcConfig,
    kind: str,
    detail: str | None,
    routes: OidcRouteConfig,
    locale: str,
) -> RedirectResponse:
    query = f"oidc_error={quote(kind)}"
    if detail:
        query += f"&oidc_error_detail={quote(detail[:300])}"
    login_path = _localized_path(routes.frontend_login_path, locale)
    response = RedirectResponse(f"{config.frontend_base_url}{login_path}?{query}", 302)
    return response


def _locale_from_next(next_path: str | None, routes: OidcRouteConfig) -> str:
    first = (next_path or "").lstrip("/").split("/", 1)[0]
    return first if first in routes.supported_locales else routes.default_locale


def _resolved_locale(value: str | None, routes: OidcRouteConfig) -> str | None:
    return value if value in routes.supported_locales else None


def _locale_from_claims(claims: dict[str, Any], routes: OidcRouteConfig) -> str:
    explicit = str(claims.get("locale") or "")
    if explicit in routes.supported_locales:
        return explicit
    return _locale_from_next(str(claims.get("next") or ""), routes)


def _localized_path(path: str, locale: str) -> str:
    return f"/{locale}{path if path.startswith('/') else '/' + path}"


def _provider_error_kind(value: str) -> str:
    """将不受信 OIDC provider error 收敛到固定的前端错误种类。"""

    return value if value in _PROVIDER_ERROR_KINDS else "provider_error"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
