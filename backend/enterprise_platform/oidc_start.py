"""OIDC 状态与授权入口路由。"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from enterprise_platform.auth import AuthError
from enterprise_platform.oidc import (
    STATE_TTL_SECONDS,
    OidcFlowError,
    OidcHost,
    OidcRouteConfig,
    _resolved_locale,
    build_authorize_url,
    issue_state,
    sanitize_next,
)
from enterprise_platform.schemas import CurrentUser, PlatformModel

_NO_END_SESSION = {"code": "NO_END_SESSION"}


class EndSessionRequest(PlatformModel):
    return_to: str | None = None


def register_oidc_start(
    router: APIRouter,
    host: OidcHost,
    route_config: OidcRouteConfig,
    current_user_dependency: Callable[..., CurrentUser] | None = None,
) -> None:
    _register_status(router, host, route_config)
    _register_authorize(router, host, route_config)
    _register_end_session(router, host, _as_http_user(current_user_dependency or _unauthenticated_user))


def _unauthenticated_user() -> CurrentUser:
    raise HTTPException(401, "未登录")


def _as_http_user(dependency: Callable[..., CurrentUser]) -> Callable[..., CurrentUser]:
    def current_user() -> CurrentUser:
        try:
            return dependency()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    return current_user


def _register_status(router: APIRouter, host: OidcHost, route_config: OidcRouteConfig) -> None:
    @router.get("/status")
    def status() -> dict[str, Any]:
        try:
            config = host.config()
            config.validated()
            enabled = True
        except OidcFlowError:
            enabled = False
        return {
            "enabled": enabled,
            "authorizePath": route_config.authorize_path,
            "silentAuthorizePath": route_config.authorize_path + "?silent=1",
            "endSessionUrl": f"{config.issuer.rstrip('/')}/end-session/" if enabled else None,
        }


def _register_authorize(router: APIRouter, host: OidcHost, route_config: OidcRouteConfig) -> None:
    @router.get("/authorize")
    def authorize(request: Request, next: str | None = Query(None, max_length=500), silent: bool = False):
        try:
            config = host.config().validated()
        except OidcFlowError as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "kind": exc.kind})
        locale = _resolved_locale(route_config.locale_resolver(request), route_config)
        state, nonce, challenge, cookie = issue_state(sanitize_next(next), config, locale=locale, silent=silent)
        response = RedirectResponse(
            build_authorize_url(config, state=state, nonce=nonce, challenge=challenge, silent=silent), 302
        )
        response.set_cookie(
            f"{route_config.state_cookie_name}_silent" if silent else route_config.state_cookie_name,
            cookie,
            max_age=STATE_TTL_SECONDS,
            path=route_config.cookie_path,
            httponly=True,
            samesite="lax",
            secure=(
                route_config.force_secure_cookies
                or request.url.scheme == "https"
                or request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"
            ),
        )
        return response


def _register_end_session(
    router: APIRouter, host: OidcHost, current_user_dependency: Callable[..., CurrentUser]
) -> None:
    @router.post("/end-session")
    def end_session(
        user: CurrentUser = Depends(current_user_dependency),
        body: EndSessionRequest | None = Body(None),
    ):
        return _end_session_payload(host, user.id, None if body is None else body.return_to)


def _end_session_payload(host: OidcHost, account_id: str, return_to: str | None) -> dict[str, Any] | JSONResponse:
    path = _validated_return_to(return_to)
    try:
        config = host.config().validated()
    except OidcFlowError:
        return JSONResponse(_NO_END_SESSION, 404)
    hint = host.end_session_hint(account_id)
    if not hint:
        return JSONResponse(_NO_END_SESSION, 404)
    fields = {"id_token_hint": hint}
    if path is not None:
        fields["post_logout_redirect_uri"] = f"{config.frontend_base_url.rstrip('/')}{path}"
    return {"url": f"{config.issuer.rstrip('/')}/end-session/", "method": "POST", "fields": fields}


def _validated_return_to(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value.startswith("/")
        or "//" in value
        or "\\" in value
        or "://" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise HTTPException(422, "returnTo 必须是以单个 / 开头的站内路径")
    return value
