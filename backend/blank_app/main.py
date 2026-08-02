"""空白企业框架站 FastAPI 入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from blank_app.adapters import (
    BlankFooterAdapter,
    BlankIntegrationAdapter,
    BlankNotificationAdapter,
    BlankUpstreamHealthAdapter,
    account_adapter,
    authorize_security_operation,
    record_platform_audit,
    request_principal_account_id,
    request_token,
    require_permission,
    seed_default_admin,
    validate_signing_secrets,
)
from blank_app.authz_api import (
    descriptor_router,
    resolve_trusted_principal,
    seed_platform_catalog,
    validate_principal_config,
)
from blank_app.authz_api import router as authz_router
from blank_app.database import SessionLocal
from blank_app.oidc_adapter import BlankOidcHost
from enterprise_platform import PlatformPorts, PlatformSecurityHooks, create_platform_router
from enterprise_platform.auth import AuthError
from enterprise_platform.authz import PrincipalValidationError
from enterprise_platform.oidc import create_oidc_router

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
}
SENSITIVE_PATH_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/users/me",
    "/api/v1/identity-integration",
    "/api/v1/authz-integration",
    "/api/v1/notifications",
    "/.well-known/easyauth-app.json",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # schema 只能由 blank_app/alembic owner 管理；启动时不调用 create_all。
    # 签名密钥门禁独立于 seed 路径：本地认证禁用（OIDC-only）时同样必须校验。
    validate_signing_secrets()
    validate_principal_config()
    seed_default_admin()
    seed_platform_catalog()
    yield


app = FastAPI(title="Enterprise Blank API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def bind_bearer_token(request: Request, call_next):
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    context_token = request_token.set(token)
    try:
        principal_account_id = resolve_trusted_principal(dict(request.headers))
    except PrincipalValidationError as exc:
        request_token.reset(context_token)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    principal_context_token = request_principal_account_id.set(principal_account_id)
    try:
        return await call_next(request)
    finally:
        request_principal_account_id.reset(principal_context_token)
        request_token.reset(context_token)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path.startswith(SENSITIVE_PATH_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AuthError)
async def auth_failure_handler(_request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


ports = PlatformPorts(
    account=account_adapter,
    footer=BlankFooterAdapter(),
    notifications=BlankNotificationAdapter(),
    integrations=BlankIntegrationAdapter(),
    upstream_health=BlankUpstreamHealthAdapter(),
    require_permission=require_permission,
)


security_hooks = PlatformSecurityHooks(
    ensure_local_auth_management_allowed=authorize_security_operation,
    after_event=record_platform_audit,
)
app.include_router(
    create_platform_router(ports, include_authz_integration=False, security_hooks=security_hooks), prefix="/api/v1"
)
app.include_router(authz_router, prefix="/api/v1")
app.include_router(descriptor_router)
app.include_router(create_oidc_router(BlankOidcHost()), prefix="/api/v1")


@app.get("/health")
@app.get("/api/v1/health")
def health() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "enterprise-blank"}
