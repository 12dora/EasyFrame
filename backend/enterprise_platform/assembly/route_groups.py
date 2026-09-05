"""按 (prefix, flag getter) 表筛选 route groups,避免宿主复制共享 route 实现。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter

from enterprise_platform.assembly.contracts import PlatformRouteGroups

_Flag = Callable[[], bool]


@dataclass(frozen=True)
class _PrefixRule:
    prefix: str
    enabled: _Flag
    exact: bool = False


def apply_route_groups(router: APIRouter, groups: PlatformRouteGroups, *, include_authz_integration: bool) -> None:
    rules = _prefix_rules(groups, include_authz_integration)
    router.routes[:] = [route for route in router.routes if _route_enabled(route.path, rules)]


def _prefix_rules(groups: PlatformRouteGroups, include_authz_integration: bool) -> tuple[_PrefixRule, ...]:
    logout = groups.auth if groups.logout is None else groups.logout
    return (
        _PrefixRule("/auth/login/passkey", lambda: groups.passkeys),
        _PrefixRule("/users/me/passkeys", lambda: groups.passkeys),
        _PrefixRule("/auth/me", lambda: groups.me, exact=True),
        _PrefixRule("/auth/logout", lambda: logout, exact=True),
        _PrefixRule("/users/me/password", lambda: groups.password, exact=True),
        _PrefixRule("/users/me/totp", lambda: groups.totp),
        _PrefixRule("/auth/", lambda: groups.auth),
        _PrefixRule("/app-settings/footer", lambda: groups.footer),
        _PrefixRule("/notifications", lambda: groups.notifications),
        _PrefixRule("/identity-integration", lambda: groups.identity),
        _PrefixRule("/authz-integration", lambda: groups.easyauth and include_authz_integration),
        _PrefixRule("/ops/upstream-health", lambda: groups.upstream),
    )


def _route_enabled(path: str, rules: tuple[_PrefixRule, ...]) -> bool:
    for rule in rules:
        matched = path == rule.prefix if rule.exact else path.startswith(rule.prefix)
        if matched:
            return rule.enabled()
    return True
