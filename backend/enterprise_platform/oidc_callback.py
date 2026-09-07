"""普通登录与静默身份复检的回调结果。"""

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from enterprise_platform import oidc

_LOGGED_OUT_ERRORS = frozenset({"login_required", "interaction_required", "consent_required", "access_denied"})


class _Callback:
    def __init__(self, host: oidc.OidcHost, routes: oidc.OidcRouteConfig):
        self.host = host
        self.routes = routes
        self.config = host.config().validated()
        self.locale = routes.default_locale
        self.claims: dict[str, Any] = {}

    @property
    def silent(self) -> bool:
        return self.claims.get("silent") is True

    def redirect(self, values: dict[str, str]) -> RedirectResponse:
        path = self.routes.frontend_silent_path if self.silent else self.routes.frontend_complete_path
        path = oidc._localized_path(path, self.locale)
        return RedirectResponse(
            f"{self.config.frontend_base_url}{path}#{urlencode(values)}", 302, headers={"Cache-Control": "no-store"}
        )

    def error(self, kind: str, detail: str | None = None, *, provider: bool = False) -> RedirectResponse:
        if self.silent:
            outcome = "logged_out" if provider and kind in _LOGGED_OUT_ERRORS else "error"
            return self.redirect({"outcome": outcome, "kind": kind})
        return oidc._error_redirect(self.config, kind, detail, self.routes, self.locale)

    def complete(self, code: str | None) -> RedirectResponse:
        if not code:
            raise oidc.OidcFlowError("回调缺少授权码", kind="missing_code")
        tokens = oidc.exchange_code(self.config, code, str(self.claims.get("verifier") or ""))
        claims = oidc.validate_id_token(self.config, str(tokens["id_token"]), str(self.claims.get("nonce") or ""))
        if not claims.get("picture"):
            claims = {**oidc.fetch_userinfo(self.config, str(tokens.get("access_token") or "")), **claims}
        account = self.host.upsert_identity(oidc.OidcIdentity.from_claims(claims))
        token = self.host.issue_session(account)
        if self.silent:
            return self.redirect({"outcome": "authenticated", "token": token, "account": account})
        return self.redirect({"token": token, "next": str(self.claims.get("next") or "")})

    def run(self, request: Request, code: str | None, state: str | None, error: str | None) -> RedirectResponse:
        cookie = request.cookies.get(self.routes.state_cookie_name)
        try:
            # 先验签选择结果页，再完整校验有效期和 state；过期 cookie 仅用于路由错误。
            self.claims = oidc._state_cookie_claims(cookie, self.config, verify_exp=False)
            self.locale = oidc._locale_from_claims(self.claims, self.routes)
            oidc.verify_state(cookie, state, self.config)
            if error:
                return self.error(oidc._provider_error_kind(error), provider=True)
            return self.complete(code)
        except oidc.OidcFlowError as exc:
            return self.error(exc.kind, None if exc.kind == "state_mismatch" else exc.detail)


def register_oidc_callback(router: APIRouter, host: oidc.OidcHost, routes: oidc.OidcRouteConfig) -> None:
    @router.get("/callback")
    def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ):
        try:
            response = _Callback(host, routes).run(request, code, state, error)
        except oidc.OidcFlowError as exc:
            response = JSONResponse({"detail": exc.detail, "kind": exc.kind}, exc.status_code)
        response.delete_cookie(routes.state_cookie_name, path=routes.cookie_path)
        return response
