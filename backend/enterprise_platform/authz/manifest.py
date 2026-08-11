"""权限目录与 EasyAuth manifest 注册的共享合同。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise_platform.authz.core import DataScope, UnsupportedDataScopeError, parse_data_scope

RiskLevel = Literal["standard", "high"]


def normalize_catalog_risk_level(value: object) -> RiskLevel:
    """目录存量未知风险等级按高风险处理。"""

    if value == "standard":
        return "standard"
    return "high"


class ManifestRegistrationError(ValueError):
    """权限目录或 manifest 注册内容不安全。"""


class PermissionRegistration(BaseModel):
    """一个企业应用向 EasyAuth 注册的权限目录项。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    group_key: str | None = None
    supported_scopes: tuple[DataScope, ...] = Field(min_length=1)
    risk_level: RiskLevel
    active: bool = True

    @field_validator("supported_scopes", mode="before")
    @classmethod
    def validate_scopes(cls, value: object) -> tuple[DataScope, ...]:
        if not isinstance(value, list | tuple) or not value:
            raise ValueError("supported_scopes must be a non-empty list")
        scopes: list[DataScope] = []
        for raw_scope in value:
            try:
                scope = parse_data_scope(str(raw_scope))
            except UnsupportedDataScopeError as exc:
                raise ValueError(str(exc)) from exc
            if scope in scopes:
                raise ValueError(f"duplicate supported scope: {scope.value}")
            scopes.append(scope)
        return tuple(scopes)


class PermissionManifestRegistration(BaseModel):
    """空白框架站可直接提供给 EasyAuth 的最小 manifest 注册合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1)
    app_key: str = Field(min_length=1)
    permissions: tuple[PermissionRegistration, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_permission_codes(self) -> PermissionManifestRegistration:
        codes = [permission.code for permission in self.permissions]
        duplicate = next((code for code in codes if codes.count(code) > 1), None)
        if duplicate is not None:
            raise ValueError(f"duplicate permission registration: {duplicate}")
        return self


class PermissionManifestRegistry:
    """校验目录唯一性，并验证 manifest grants 未越出目录声明。"""

    def __init__(self, permissions: Iterable[PermissionRegistration] = ()) -> None:
        self._permissions: dict[str, PermissionRegistration] = {}
        for permission in permissions:
            self.register(permission)

    def register(self, permission: PermissionRegistration) -> None:
        if permission.code in self._permissions:
            raise ManifestRegistrationError(f"duplicate permission registration: {permission.code}")
        self._permissions[permission.code] = permission

    def validate_grant(self, permission_code: str, scope: str | DataScope) -> DataScope:
        permission = self._permissions.get(permission_code)
        if permission is None:
            raise ManifestRegistrationError(f"unknown grant permission: {permission_code}")
        try:
            parsed_scope = parse_data_scope(scope)
        except UnsupportedDataScopeError as exc:
            raise ManifestRegistrationError(f"unsupported grant scope: {permission_code}:{scope}") from exc
        if parsed_scope not in permission.supported_scopes:
            raise ManifestRegistrationError(f"unsupported grant scope: {permission_code}:{parsed_scope.value}")
        return parsed_scope

    def supported_scopes(self, permission_code: str) -> frozenset[DataScope]:
        permission = self._permissions.get(permission_code)
        if permission is None:
            raise ManifestRegistrationError(f"unknown permission registration: {permission_code}")
        return frozenset(permission.supported_scopes)
