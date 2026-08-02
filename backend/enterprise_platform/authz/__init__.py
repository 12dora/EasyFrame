"""可复用 EasyAuth 授权核心。"""

from enterprise_platform.authz.client import (
    EasyAuthClientError,
    EasyAuthForbiddenError,
    EasyAuthPermissionClient,
)
from enterprise_platform.authz.connection import ConnectionErrorKind, classify_connection_failure
from enterprise_platform.authz.core import (
    CatalogPermission,
    DataScope,
    EasyAuthGrantItem,
    EasyAuthGrantResolved,
    EasyAuthGroupItem,
    EasyAuthPermissionSnapshot,
    NormalizedGrant,
    UnsupportedDataScopeError,
    allowed_external_user_ids,
    best_scope_for_grants,
    normalize_grants,
    parse_data_scope,
    parse_grants_isolated,
    subject_ids_for_scope,
    union_subject_ids_for_permission,
)
from enterprise_platform.authz.manifest import (
    ManifestRegistrationError,
    PermissionManifestRegistration,
    PermissionManifestRegistry,
    PermissionRegistration,
)
from enterprise_platform.authz.principal import (
    PrincipalValidationError,
    UpstreamPrincipal,
    parse_upstream_principal_from_headers,
)

__all__ = [
    "CatalogPermission",
    "ConnectionErrorKind",
    "DataScope",
    "EasyAuthClientError",
    "EasyAuthForbiddenError",
    "EasyAuthGrantItem",
    "EasyAuthGrantResolved",
    "EasyAuthGroupItem",
    "EasyAuthPermissionSnapshot",
    "EasyAuthPermissionClient",
    "ManifestRegistrationError",
    "NormalizedGrant",
    "PermissionManifestRegistration",
    "PermissionManifestRegistry",
    "PermissionRegistration",
    "PrincipalValidationError",
    "UnsupportedDataScopeError",
    "UpstreamPrincipal",
    "allowed_external_user_ids",
    "best_scope_for_grants",
    "classify_connection_failure",
    "normalize_grants",
    "parse_data_scope",
    "parse_grants_isolated",
    "parse_upstream_principal_from_headers",
    "subject_ids_for_scope",
    "union_subject_ids_for_permission",
]
