"""OIDC 状态与授权入口路由。"""

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

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


def register_oidc_start(router: APIRouter, host: OidcHost, route_config: OidcRouteConfig) -> None:
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
            route_config.state_cookie_name,
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
