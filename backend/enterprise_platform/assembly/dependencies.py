"""Assembly 依赖上下文:闭包只在这里建一次,路由模块消费冻结对象。"""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable, Mapping
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
    """默认权限依赖:与 ``AssemblyDependencies.current_user`` 共用 FastAPI 缓存键。"""

    ports: PlatformPorts
    code: str
    current_user: CurrentUserDep

    def __call__(self, request: Request, user: CurrentUser) -> None:
        _enforce_permission(self.ports, self.code, request, user)

    def as_dependency(self) -> Callable[..., None]:
        current_user = self.current_user

        def check(
            request: Request,
            user: CurrentUser = Depends(current_user),
        ) -> None:
            self(request, user)

        return check


def _make_permission_for(
    ports: PlatformPorts, factory: PermissionFactory | None, current_user_dep: CurrentUserDep
) -> PermissionFactory:
    def permission_for(code: str) -> Callable[..., Any]:
        if factory is not None:
            return factory(code)
        return _FixedPermission(ports, code, current_user_dep).as_dependency()

    return permission_for


def _enforce_permission(ports: PlatformPorts, code: str, request: Request, user: CurrentUser) -> None:
    if code in user.permissions:
        return
    _deny_permission(ports, code, request, user)


def _deny_permission(ports: PlatformPorts, code: str, request: Request, user: CurrentUser) -> None:
    try:
        _call_require_permission(ports.require_permission, code, request, user)
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    raise HTTPException(403, "缺少权限")


def _call_require_permission(port: Any, code: str, request: Request, user: CurrentUser) -> None:
    """按缓存的签名约定调用;绑定 TypeError 才回退,宿主函数体不得重跑。"""

    convention = _cached_port_convention(port)
    try:
        port(code, **_permission_port_kwargs(request, user, convention))
    except TypeError as exc:
        fallback = _fallback_without_user(convention, exc)
        if fallback is None:
            raise
        _store_port_convention(port, fallback)
        port(code, **_permission_port_kwargs(request, user, fallback))


_ACCEPTED_PARAM_KINDS = frozenset({inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY})
_BINDING_TYPE_ERROR_MARKERS = ("unexpected keyword argument", "positional argument")


@dataclass(frozen=True)
class _PortCallConvention:
    pass_request: bool
    pass_user: bool


_WEAK_CONVENTIONS: weakref.WeakKeyDictionary[Any, _PortCallConvention] = weakref.WeakKeyDictionary()
_ID_CONVENTIONS: dict[int, _PortCallConvention] = {}


def _permission_port_kwargs(request: Request, user: CurrentUser, convention: _PortCallConvention) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if convention.pass_request:
        kwargs["request"] = request
    if convention.pass_user:
        kwargs["user"] = user
    return kwargs


def _has_explicit_param(parameters: Mapping[str, inspect.Parameter], name: str) -> bool:
    param = parameters.get(name)
    return param is not None and param.kind in _ACCEPTED_PARAM_KINDS


def _inspect_port_convention(port: Any) -> _PortCallConvention:
    try:
        parameters = inspect.signature(port).parameters
    except (TypeError, ValueError):
        return _PortCallConvention(pass_request=False, pass_user=False)
    return _PortCallConvention(
        pass_request=_has_explicit_param(parameters, "request"),
        pass_user=_has_explicit_param(parameters, "user"),
    )


def _lookup_port_convention(port: Any) -> _PortCallConvention | None:
    try:
        return _WEAK_CONVENTIONS.get(port)
    except TypeError:
        return _ID_CONVENTIONS.get(id(port))


def _store_port_convention(port: Any, convention: _PortCallConvention) -> None:
    try:
        _WEAK_CONVENTIONS[port] = convention
    except TypeError:
        _ID_CONVENTIONS[id(port)] = convention


def _cached_port_convention(port: Any) -> _PortCallConvention:
    cached = _lookup_port_convention(port)
    if cached is not None:
        return cached
    convention = _inspect_port_convention(port)
    _store_port_convention(port, convention)
    return convention


def _is_binding_type_error(exc: TypeError) -> bool:
    if not any(marker in str(exc) for marker in _BINDING_TYPE_ERROR_MARKERS):
        return False
    traceback = exc.__traceback__
    return traceback is not None and traceback.tb_next is None


def _fallback_without_user(convention: _PortCallConvention, exc: TypeError) -> _PortCallConvention | None:
    if not convention.pass_user or not _is_binding_type_error(exc):
        return None
    return _PortCallConvention(pass_request=convention.pass_request, pass_user=False)


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
    gated = _make_gated_current_user(recovery)
    admit_login = _LoginAdmission(hooks, enforce_shared_rate_limits)
    return AssemblyDependencies(
        ports=ports,
        hooks=hooks,
        current_user=gated,
        recovery_user=recovery,
        permission_for=_make_permission_for(ports, permission_dependency_factory, gated),
        port_call=port_call,
        audited_connection_test=_AuditedConnectionTest(hooks),
        admit_login=admit_login,
        admit_second_factor=_SecondFactorAdmission(hooks, enforce_shared_rate_limits),
        login_guard=LoginAdmissionGuard(admit_login),
        include_authz_integration=include_authz_integration,
    )
