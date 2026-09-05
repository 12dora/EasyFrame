"""Assembly 依赖上下文:闭包只在这里建一次,路由模块消费冻结对象。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request

from enterprise_platform.assembly.contracts import PlatformPorts, PlatformSecurityHooks
from enterprise_platform.auth import AuthError
from enterprise_platform.login_flow import LoginAdmissionGuard
from enterprise_platform.rate_limit import (
    clear_second_factor_failures,
    enforce_login,
    enforce_second_factor,
    record_second_factor_failure,
    second_factor_failure_admission,
)
from enterprise_platform.schemas import ConnectionTestResult, CurrentUser

PortCall = Callable[[Callable[[], Any]], Any]
PermissionFactory = Callable[[str], Callable[..., Any]]
CurrentUserDep = Callable[..., CurrentUser]
LoginAdmit = Callable[[str, Request], None]
SecondFactorAdmit = Callable[[str, str, Request], None]


def port_call(callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


# eq=False:FastAPI 以依赖可调用对象为缓存键,生成的 __hash__ 会递归哈希 ports/adapter,
# 宿主传入可变 adapter 时会 TypeError;按身份哈希与原闭包行为一致。
@dataclass(frozen=True, eq=False)
class _FixedCurrentUser:
    ports: PlatformPorts

    def __call__(self) -> CurrentUser:
        try:
            return self.ports.account.current_user()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc


def _make_recovery_user(resolved: CurrentUserDep) -> CurrentUserDep:
    def recovery_user(user: CurrentUser = Depends(resolved)) -> CurrentUser:
        return user

    return recovery_user


def _make_gated_current_user(recovery_user: CurrentUserDep) -> CurrentUserDep:
    def current_user(user: CurrentUser = Depends(recovery_user)) -> CurrentUser:
        if user.must_change_password:
            raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED"})
        return user

    return current_user


@dataclass(frozen=True, eq=False)
class _FixedPermission:
    ports: PlatformPorts
    code: str

    def __call__(self, request: Request) -> None:
        try:
            # 透传 Request,便于拒绝审计记录 method/route。
            self.ports.require_permission(self.code, request)  # type: ignore[call-arg]
        except TypeError:
            self._without_request()
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def _without_request(self) -> None:
        try:
            self.ports.require_permission(self.code)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc


def _make_permission_for(ports: PlatformPorts, factory: PermissionFactory | None) -> PermissionFactory:
    def permission_for(code: str) -> Callable[..., Any]:
        if factory is not None:
            return factory(code)
        return _FixedPermission(ports, code)

    return permission_for


@dataclass(frozen=True)
class _AuditedConnectionTest:
    hooks: PlatformSecurityHooks

    def __call__(
        self, callback: Callable[[], ConnectionTestResult], *, actor_id: str, action: str
    ) -> ConnectionTestResult:
        try:
            result = port_call(callback)
        except HTTPException:
            self.hooks.after_event(actor_id, action, None, {"ok": False, "latencyMs": 0, "errorKind": "request_failed"})
            raise
        self.hooks.after_event(
            actor_id,
            action,
            None,
            {"ok": result.ok, "latencyMs": result.latency_ms, "errorKind": result.error_kind},
        )
        return result


@dataclass(frozen=True)
class _LoginAdmission:
    hooks: PlatformSecurityHooks
    enforce_shared: bool

    def __call__(self, username: str, request: Request) -> None:
        """登录准入:共享限流(可关闭)+ 宿主自有限流 hook,两者都在认证之前。"""

        try:
            if self.enforce_shared:
                enforce_login(request, username)
            self.hooks.before_password_login(username, request)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc


@dataclass(frozen=True)
class _SecondFactorAdmission:
    hooks: PlatformSecurityHooks
    enforce_shared: bool

    def __call__(self, account_id: str, operation: str, request: Request) -> None:
        """二次验证准入:共享限流(可关闭)+ 宿主自有限流 hook。"""

        try:
            if self.enforce_shared:
                enforce_second_factor(request, account_id)
            self.hooks.before_second_factor(account_id, operation, request)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc


@dataclass(frozen=True)
class AssemblyDependencies:
    ports: PlatformPorts
    hooks: PlatformSecurityHooks
    current_user: CurrentUserDep
    recovery_user: CurrentUserDep
    permission_for: PermissionFactory
    port_call: PortCall
    audited_connection_test: _AuditedConnectionTest
    admit_login: LoginAdmit
    admit_second_factor: SecondFactorAdmit
    login_guard: LoginAdmissionGuard
    include_authz_integration: bool

    def run_reverified_mutation(
        self,
        request: Request,
        user_id: str,
        operation: str,
        check: Callable[[], bool],
        error_detail: str,
    ) -> None:
        with second_factor_failure_admission(user_id):
            self.admit_second_factor(user_id, operation, request)
            if not self.port_call(check):
                record_second_factor_failure(user_id)
                raise HTTPException(401, error_detail)
            clear_second_factor_failures(user_id)


def build_assembly_dependencies(
    ports: PlatformPorts,
    *,
    hooks: PlatformSecurityHooks,
    current_user_dependency: CurrentUserDep | None,
    permission_dependency_factory: PermissionFactory | None,
    enforce_shared_rate_limits: bool,
    include_authz_integration: bool,
) -> AssemblyDependencies:
    resolved = current_user_dependency or _FixedCurrentUser(ports)
    recovery = _make_recovery_user(resolved)
    admit_login = _LoginAdmission(hooks, enforce_shared_rate_limits)
    return AssemblyDependencies(
        ports=ports,
        hooks=hooks,
        current_user=_make_gated_current_user(recovery),
        recovery_user=recovery,
        permission_for=_make_permission_for(ports, permission_dependency_factory),
        port_call=port_call,
        audited_connection_test=_AuditedConnectionTest(hooks),
        admit_login=admit_login,
        admit_second_factor=_SecondFactorAdmission(hooks, enforce_shared_rate_limits),
        login_guard=LoginAdmissionGuard(admit_login),
        include_authz_integration=include_authz_integration,
    )
