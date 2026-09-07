"""OIDC 后通道注销：验签后按上游 subject 撤销宿主会话。"""

import logging
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.concurrency import run_in_threadpool

from enterprise_platform.oidc import OidcConfig, OidcFlowError, OidcHost, _signing_key

_LOG = logging.getLogger(__name__)
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


def _logout_subject(claims: dict[str, Any]) -> str:
    events = claims.get("events")
    if not isinstance(events, dict) or LOGOUT_EVENT not in events:
        raise ValueError("logout_token 缺少 backchannel-logout events")
    if "nonce" in claims:
        raise ValueError("logout_token 不得包含 nonce")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise ValueError("logout_token 必须包含 sub; sid-only logout is unsupported")
    if not isinstance(claims.get("jti"), str) or not claims["jti"].strip():
        raise ValueError("logout_token 缺少有效 jti")
    issued = claims["iat"]
    if isinstance(issued, bool) or not isinstance(issued, (int, float)) or not issued <= time.time():
        raise ValueError("logout_token iat 无效")
    if "exp" not in claims and issued < time.time() - 300:
        raise ValueError("logout_token iat 已超过 300 秒且没有 exp")
    return sub


def validate_logout_token(config: OidcConfig, token: str) -> str:
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"}:
            raise ValueError("logout_token 算法不受支持")
        key = _signing_key(config, header.get("kid"))
        if key.get("use") not in {None, "sig"} or key.get("alg") not in {None, algorithm}:
            raise ValueError("logout_token 签名密钥用途不匹配")
        claims = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=config.client_id,
            issuer=config.issuer,
            options={"require_iat": True, "require_jti": True, "require_aud": True, "require_iss": True},
        )
        return _logout_subject(claims)
    except (JWTError, ValueError, TypeError, OverflowError) as exc:
        raise OidcFlowError("logout_token 校验失败: " + str(exc), kind="invalid_request") from exc


async def _form_token(request: Request) -> str:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
        raise ValueError("需要 application/x-www-form-urlencoded")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 64 * 1024:
            raise ValueError("logout_token 请求过大")
    fields = parse_qs(body.decode("utf-8"), max_num_fields=20)
    tokens = fields.get("logout_token", [])
    if len(tokens) != 1 or not tokens[0]:
        raise ValueError("必须提供一个 logout_token")
    return tokens[0]


def _process_logout(host: OidcHost, token: str) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    try:
        config = host.config().validated()
    except OidcFlowError as exc:
        return JSONResponse({"detail": exc.detail, "kind": exc.kind}, exc.status_code, headers=headers)
    try:
        sub = validate_logout_token(config, token)
    except OidcFlowError as exc:
        return _invalid_request(exc.detail)
    # 框架没有共享缓存，不保存 jti 防重放；重复请求仍执行按账号撤销。
    revoked = host.revoke_sessions_by_subject(sub)
    _LOG.info("OIDC backchannel logout subject=%r revoked_accounts=%d", sub, revoked)
    return JSONResponse({}, headers=headers)


def _invalid_request(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_request", "error_description": detail}, 400, headers={"Cache-Control": "no-store"}
    )


def register_backchannel_logout(router: APIRouter, host: OidcHost) -> None:
    @router.post("/backchannel-logout")
    async def backchannel_logout(request: Request):
        try:
            token = await _form_token(request)
        except ValueError as exc:
            return _invalid_request(str(exc))
        return await run_in_threadpool(_process_logout, host, token)
