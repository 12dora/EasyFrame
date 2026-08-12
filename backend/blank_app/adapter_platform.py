"""blank host 页脚、通知、身份集成与上游健康适配。"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import and_, or_

from blank_app.adapter_support import (
    _allow_local_outbound,
    _decrypt_saved_secret,
    _get_setting,
    _save_setting,
)
from blank_app.database import SessionLocal
from blank_app.models import HealthSnapshot, Notification, PlatformAuditLog, PlatformSetting
from enterprise_platform.auth import AuthError
from enterprise_platform.authz import (
    EasyAuthClientError,
    EasyAuthForbiddenError,
    EasyAuthPermissionClient,
    classify_connection_failure,
)
from enterprise_platform.footer import sanitize_footer_html
from enterprise_platform.health import safe_health_summary
from enterprise_platform.jwks import probe_jwks
from enterprise_platform.oidc_settings import (
    normalize_base_url,
    normalize_oidc_settings,
    oidc_client_authority,
    rewrite_for_server_side,
)
from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request
from enterprise_platform.schemas import (
    ConnectionTestResult,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    FooterSettings,
    IdentityDiscoveryResponse,
    NotificationItem,
    NotificationPage,
    OidcSettings,
    OidcSettingsUpdate,
    PermissionRequestUrlUpdate,
    UpstreamHealthItem,
    UserSyncCapabilityResponse,
)
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret, encrypt_secret


def _facade():
    import blank_app.adapters as adapters

    return adapters


class BlankFooterAdapter:
    def get_footer(self) -> FooterSettings:
        with SessionLocal() as db:
            row = db.get(PlatformSetting, "footer")
            if row is None:
                return FooterSettings(footer_html_zh="企业应用 · © {year}", footer_html_en="Enterprise App · © {year}")
            return FooterSettings.model_validate(row.value)

    def save_footer(self, footer: FooterSettings, *, actor_id: str) -> FooterSettings:
        safe = FooterSettings(
            footer_html_zh=sanitize_footer_html(footer.footer_html_zh),
            footer_html_en=sanitize_footer_html(footer.footer_html_en),
        )
        with SessionLocal() as db:
            row = db.get(PlatformSetting, "footer") or PlatformSetting(key="footer")
            before = dict(row.value) if row.value else None
            row.value = safe.model_dump(mode="json")
            db.add(row)
            db.add(
                PlatformAuditLog(
                    actor_id=actor_id,
                    action="settings.footer.update",
                    before_data=before,
                    after_data=safe.model_dump(mode="json"),
                )
            )
            db.commit()
        return safe


class BlankNotificationAdapter:
    def list_notifications(self, user_id: str, *, cursor: str | None, limit: int) -> NotificationPage:
        with SessionLocal() as db:
            query = db.query(Notification).filter(Notification.account_id == user_id)
            if cursor:
                created_at, notification_id = _decode_notification_cursor(cursor)
                query = query.filter(
                    or_(
                        Notification.created_at < created_at,
                        and_(Notification.created_at == created_at, Notification.id < notification_id),
                    )
                )
            rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1).all()
            items = [
                NotificationItem(
                    id=str(row.id),
                    title=row.title,
                    body=row.body,
                    level=row.level,
                    created_at=row.created_at,
                    read_at=row.read_at,
                    href=row.href,
                )
                for row in rows[:limit]
            ]
            unread = (
                db.query(Notification.id)
                .filter(Notification.account_id == user_id, Notification.read_at.is_(None))
                .count()
            )
            return NotificationPage(
                items=items,
                unread_count=unread,
                next_cursor=_encode_notification_cursor(rows[limit - 1]) if len(rows) > limit else None,
            )

    def mark_read(self, user_id: str, notification_id: str, *, read_at: datetime) -> bool:
        with SessionLocal() as db:
            row = (
                db.query(Notification)
                .filter(Notification.account_id == user_id, Notification.id == notification_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.read_at = read_at
            db.commit()
            return True

    def mark_all_read(self, user_id: str, *, read_at: datetime) -> int:
        with SessionLocal() as db:
            count = (
                db.query(Notification)
                .filter(Notification.account_id == user_id, Notification.read_at.is_(None))
                .update({Notification.read_at: read_at})
            )
            db.commit()
            return count


def _encode_notification_cursor(row: Notification) -> str:
    payload = f"{_as_utc(row.created_at).isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_notification_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        created_at_text, notification_id_text = decoded.rsplit("|", 1)
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return created_at.astimezone(UTC), uuid.UUID(notification_id_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthError(400, "通知游标无效") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)

class BlankIntegrationAdapter:
    def get_oidc_settings(self) -> OidcSettings:
        data = _get_setting("oidc")
        return OidcSettings.model_validate({**data, "user_sync_supported": False})

    def save_oidc_settings(self, payload: OidcSettingsUpdate, *, actor_id: str) -> OidcSettings:
        payload = normalize_oidc_settings(payload)
        old = _get_setting("oidc")
        old_client_authority = oidc_client_authority(old)
        new_client_authority = oidc_client_authority(payload)
        client_authority_changed = old_client_authority.has_values() and old_client_authority != new_client_authority
        api_authority_changed = bool(old.get("authentik_api_base_url")) and (
            normalize_base_url(str(old.get("authentik_api_base_url") or "")) != payload.authentik_api_base_url
        )
        client_secret = payload.client_secret
        if client_authority_changed and not client_secret:
            raise AuthError(422, "身份提供方 authority 或 clientId 变更时必须重新填写 clientSecret")
        if client_secret is None:
            client_secret = _decrypt_saved_secret(old.get("client_secret"))
        authentik_api_token = payload.authentik_api_token
        if api_authority_changed and not authentik_api_token:
            raise AuthError(422, "Authentik API authority 变更时必须重新填写 API token")
        if authentik_api_token is None:
            authentik_api_token = _decrypt_saved_secret(old.get("authentik_api_token"))
        data = payload.model_dump(mode="json", exclude={"client_secret", "authentik_api_token"})
        data["has_client_secret"] = bool(client_secret)
        data["has_authentik_api_token"] = bool(authentik_api_token)
        data["client_secret"] = encrypt_secret(client_secret or "")
        data["authentik_api_token"] = encrypt_secret(authentik_api_token or "")
        data["redirect_uri"] = (
            f"{payload.redirect_base_url.rstrip('/')}/api/v1/auth/oidc/callback" if payload.redirect_base_url else ""
        )
        _save_setting("oidc", data, actor_id=actor_id, action="identity.settings.update")
        return OidcSettings.model_validate({**data, "user_sync_supported": False})

    def test_oidc(self) -> ConnectionTestResult:
        configured = self.get_oidc_settings()
        server_base_url = configured.server_base_url.strip()
        return probe_jwks(
            rewrite_for_server_side(server_base_url, configured.jwks_uri),
            allow_localhost=_allow_local_outbound(),
            transport=(
                _facade().create_trusted_authority_transport(server_base_url) if server_base_url else guarded_request
            ),
        )

    def discover_oidc(self, issuer: str | None) -> IdentityDiscoveryResponse:
        configured = self.get_oidc_settings()
        target = (issuer or configured.issuer).strip().rstrip("/")
        if not target:
            return IdentityDiscoveryResponse(ok=False, error_kind="not_configured", error_detail="issuer 不能为空")
        server_base_url = configured.server_base_url.strip()
        url = rewrite_for_server_side(server_base_url, f"{target}/.well-known/openid-configuration")
        transport = (
            _facade().create_trusted_authority_transport(server_base_url) if server_base_url else guarded_request
        )
        try:
            response = transport("GET", url, timeout=5, allow_localhost=_allow_local_outbound())
        except UnsafeOutboundUrlError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="blocked", error_detail=str(exc))
        except httpx.HTTPError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="unreachable", error_detail=str(exc)[:300])
        if response.status_code != 200:
            return IdentityDiscoveryResponse(
                ok=False, error_kind="http_error", error_detail=f"discovery 返回 {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            return IdentityDiscoveryResponse(ok=False, error_kind="invalid_response", error_detail="响应不是 JSON")
        required = ("authorization_endpoint", "token_endpoint", "jwks_uri")
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
            return IdentityDiscoveryResponse(
                ok=False, error_kind="invalid_response", error_detail="discovery 缺少必要端点"
            )
        try:
            for key in required:
                from enterprise_platform.urls import validate_endpoint_url

                validate_endpoint_url(str(payload[key]))
        except ValueError as exc:
            return IdentityDiscoveryResponse(ok=False, error_kind="invalid_response", error_detail=str(exc))
        return IdentityDiscoveryResponse(
            ok=True,
            issuer=str(payload.get("issuer") or target),
            authorization_endpoint=str(payload["authorization_endpoint"]),
            token_endpoint=str(payload["token_endpoint"]),
            jwks_uri=str(payload["jwks_uri"]),
            userinfo_endpoint=str(payload.get("userinfo_endpoint") or ""),
        )

    def sync_identity_users(self, *, actor_id: str) -> UserSyncCapabilityResponse:
        return UserSyncCapabilityResponse(
            supported=False,
            status="not_supported",
            summary="blank host 仅在登录时维护最小用户投影，不提供权威目录生命周期同步",
        )

    def get_easyauth_status(self) -> EasyAuthStatus:
        data = _get_setting("easyauth")
        return EasyAuthStatus.model_validate(data or {})

    def save_easyauth_settings(self, payload: EasyAuthSettingsUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _get_setting("easyauth")
        authority_changed = bool(old.get("base_url") or old.get("app_key")) and (
            old.get("base_url", "").rstrip("/") != payload.base_url.rstrip("/")
            or old.get("app_key", "") != payload.app_key
        )
        credential = payload.credential
        if authority_changed and not credential:
            raise AuthError(422, "EasyAuth authority 或 appKey 变更时必须重新填写 credential")
        if credential is None:
            credential = _decrypt_saved_secret(old.get("credential"))
        credential = credential or ""
        data = {
            "configured": bool(payload.base_url and payload.app_key and credential),
            "base_url": payload.base_url.rstrip("/"),
            "app_key": payload.app_key,
            "auth_mode": "static_app_token",
            "has_credential": bool(credential),
            "permission_request_url": payload.permission_request_url,
            "credential": encrypt_secret(credential),
        }
        _save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return EasyAuthStatus.model_validate(data)

    def save_permission_request_url(self, payload: PermissionRequestUrlUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _get_setting("easyauth")
        data = dict(old)
        data["permission_request_url"] = payload.permission_request_url.strip()
        _save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return EasyAuthStatus.model_validate(data)

    def test_easyauth(self) -> ConnectionTestResult:
        data = _get_setting("easyauth")
        try:
            credential = decrypt_secret(str(data.get("credential") or ""))
        except SecretConfigurationError as exc:
            return ConnectionTestResult(ok=False, error_kind="configuration", error_detail=str(exc))
        client = EasyAuthPermissionClient(
            base_url=str(data.get("base_url") or ""),
            app_key=str(data.get("app_key") or "enterprise-blank"),
            auth_mode=str(data.get("auth_mode") or "static_app_token"),
            credential=credential,
            timeout=5,
        )
        try:
            snapshot = client.fetch_permission_snapshot("enterprise-platform-connectivity-probe")
        except EasyAuthForbiddenError as exc:
            kind = classify_connection_failure(str(exc), forbidden=True)
            return ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        except EasyAuthClientError as exc:
            kind = classify_connection_failure(
                str(exc),
                network_error=isinstance(exc.__cause__, httpx.RequestError),
            )
            return ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        finally:
            client.close()
        return ConnectionTestResult(ok=True, error_detail=f"snapshot {snapshot.snapshot_version}")


class BlankUpstreamHealthAdapter:
    probes = {
        "authentik": ("Authentik(SSO 登录)", True, lambda: BlankIntegrationAdapter().test_oidc()),
        "authentik_directory": ("Authentik 用户同步", False, None),
        "easyauth": ("EasyAuth(权限授权)", True, lambda: BlankIntegrationAdapter().test_easyauth()),
        "scheduler": ("定时任务调度器", False, None),
    }

    def latest(self) -> list[UpstreamHealthItem]:
        with SessionLocal() as db:
            result: list[UpstreamHealthItem] = []
            for dependency, (display_name, supported, _) in self.probes.items():
                row = (
                    db.query(HealthSnapshot)
                    .filter(HealthSnapshot.dependency == dependency)
                    .order_by(HealthSnapshot.checked_at.desc())
                    .first()
                )
                result.append(
                    UpstreamHealthItem(
                        dependency=dependency,
                        display_name=display_name,
                        status=row.status if row else "unknown",
                        checked_at=row.checked_at if row else None,
                        summary=safe_health_summary(row.summary) if row else "尚未记录健康快照",
                        error_summary=safe_health_summary(row.error_summary) if row else "",
                        summary_code=(_health_summary_code(row.status, row.summary) if row else "upstream.not_checked")
                        if supported
                        else "upstream.not_supported",
                        supported=supported,
                    )
                )
            return result

    def run_checks(self, *, actor_id: str) -> list[UpstreamHealthItem]:
        with SessionLocal() as db:
            for dependency, (display_name, supported, probe) in self.probes.items():
                if not supported or probe is None:
                    result = ConnectionTestResult(
                        ok=False, error_kind="not_supported", error_detail="capability is not supported by this host"
                    )
                else:
                    result = probe()
                db.add(
                    HealthSnapshot(
                        dependency=dependency,
                        display_name=display_name,
                        status="healthy"
                        if result.ok
                        else ("unknown" if result.error_kind in {"not_configured", "not_supported"} else "unhealthy"),
                        summary=(
                            "连接正常"
                            if result.ok
                            else ("当前宿主不支持此能力" if not supported else f"连接测试失败({result.error_kind})")
                        ),
                        error_summary=result.error_detail or "",
                    )
                )
            db.commit()
        return self.latest()


def _health_summary_code(status: str, summary: str) -> str:
    if "不支持" in summary:
        return "upstream.not_supported"
    if status == "healthy":
        return "upstream.healthy"
    if status == "warning":
        return "upstream.warning"
    if status == "unhealthy":
        return "upstream.unhealthy"
    return "upstream.unknown"
