"""普通登录与静默身份复检的回调结果。"""

import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from enterprise_platform import oidc

_LOGGED_OUT_ERRORS = frozenset({"login_required", "access_denied"})


class _Callback:
    def __init__(self, host: oidc.OidcHost, routes: oidc.OidcRouteConfig):
        self.host = host
        self.routes = routes
        self.config = host.config()
        self.cookie_name: str | None = None
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

    def select_cookie(self, request: Request, state: str | None) -> str | None:
        silent_cookie = None
        silent_claims: dict[str, Any] = {}
        for name in (f"{self.routes.state_cookie_name}_silent", self.routes.state_cookie_name):
            cookie = request.cookies.get(name)
            try:
                claims = oidc._state_cookie_claims(cookie, self.config, verify_exp=False)
            except oidc.OidcFlowError:
                continue
            if name == f"{self.routes.state_cookie_name}_silent":
                if claims.get("silent") is not True:
                    continue
                silent_cookie, silent_claims = cookie, claims
            if state and secrets.compare_digest(str(claims.get("state")), state):
                self.cookie_name, self.claims = name, claims
                return cookie
        # 多标签页静默检查仍可能互相覆盖；未匹配时返回 error，下一次定时检查重试。
        # 仅用验签后的静默声明选择错误页，不删除任何未匹配事务的 cookie。
        self.claims = silent_claims
        return silent_cookie

    def run(self, request: Request, code: str | None, state: str | None, error: str | None) -> RedirectResponse:
        cookie = self.select_cookie(request, state)
        self.config.validated()
        try:
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
        flow = None
        try:
            flow = _Callback(host, routes)
            response = flow.run(request, code, state, error)
        except oidc.OidcFlowError as exc:
            response = JSONResponse({"detail": exc.detail, "kind": exc.kind}, exc.status_code)
        if flow is not None and flow.cookie_name is not None:
            response.delete_cookie(flow.cookie_name, path=routes.cookie_path)
        return response
