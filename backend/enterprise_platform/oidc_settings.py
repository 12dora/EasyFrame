"""OIDC 配置文本与 authority 的共享归一化规则。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from enterprise_platform.schemas import OidcSettingsUpdate


@dataclass(frozen=True)
class OidcClientAuthority:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_id: str

    def has_values(self) -> bool:
        return any((self.issuer, self.authorization_endpoint, self.token_endpoint, self.jwks_uri, self.client_id))


def normalize_oidc_settings(payload: OidcSettingsUpdate) -> OidcSettingsUpdate:
    """返回规范化副本；两个宿主保存和 authority 比较必须调用同一入口。"""

    return payload.model_copy(
        update={
            "issuer": normalize_issuer(payload.issuer),
            "authorization_endpoint": normalize_endpoint(payload.authorization_endpoint),
            "token_endpoint": normalize_endpoint(payload.token_endpoint),
            "jwks_uri": normalize_endpoint(payload.jwks_uri),
            "userinfo_endpoint": normalize_endpoint(payload.userinfo_endpoint),
            "client_id": payload.client_id.strip(),
            "client_secret": _optional_secret(payload.client_secret),
            "scopes": normalize_scopes(payload.scopes),
            "redirect_base_url": normalize_base_url(payload.redirect_base_url),
            "frontend_base_url": normalize_base_url(payload.frontend_base_url),
            "server_base_url": normalize_base_url(payload.server_base_url),
            "authentik_api_base_url": normalize_base_url(payload.authentik_api_base_url),
            "authentik_api_token": _optional_secret(payload.authentik_api_token),
        }
    )


def oidc_client_authority(value: OidcSettingsUpdate | Mapping[str, Any]) -> OidcClientAuthority:
    """以规范值生成客户端凭据 authority，忽略纯空白与尾斜杠差异。"""

    return OidcClientAuthority(
        issuer=normalize_issuer(_read(value, "issuer")),
        authorization_endpoint=normalize_endpoint(_read(value, "authorization_endpoint")),
        token_endpoint=normalize_endpoint(_read(value, "token_endpoint")),
        jwks_uri=normalize_endpoint(_read(value, "jwks_uri")),
        client_id=_read(value, "client_id").strip(),
    )


def normalize_issuer(value: str) -> str:
    text = value.strip()
    return f"{text.rstrip('/')}/" if text else ""


def normalize_endpoint(value: str) -> str:
    return value.strip()


def normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def normalize_scopes(value: str) -> str:
    return " ".join(value.split())


def rewrite_for_server_side(server_base_url: str, url: str) -> str:
    """以显式 serverBaseUrl 改写后端出站 URL 的 authority，保留路径与查询。"""

    override = normalize_base_url(server_base_url)
    if not override or not url:
        return url
    source = urlsplit(url)
    target = urlsplit(override)
    if target.scheme not in {"http", "https"} or not target.netloc or target.username or target.password:
        return url
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


def _optional_secret(value: str | None) -> str | None:
    return None if value is None else value.strip()


def _read(value: OidcSettingsUpdate | Mapping[str, Any], field: str) -> str:
    if isinstance(value, Mapping):
        raw = value.get(field, "")
    else:
        raw = getattr(value, field)
    return str(raw or "")


__all__ = [
    "OidcClientAuthority",
    "normalize_base_url",
    "normalize_endpoint",
    "normalize_issuer",
    "normalize_oidc_settings",
    "normalize_scopes",
    "oidc_client_authority",
    "rewrite_for_server_side",
]
