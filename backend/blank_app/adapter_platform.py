"""blank host 页脚、通知、身份集成与上游健康适配。"""

from __future__ import annotations

import uuid
from datetime import datetime

from blank_app.models import Notification
from enterprise_platform.schemas import (
    ConnectionTestResult,
    DirectorySettings,
    DirectorySettingsUpdate,
    DirectorySyncResult,
    EasyAuthSettingsUpdate,
    EasyAuthStatus,
    FooterSettings,
    IdentityDiscoveryResponse,
    NotificationPage,
    OidcSettings,
    OidcSettingsUpdate,
    PermissionRequestUrlUpdate,
    UpstreamHealthItem,
)


def _facade():
    from blank_app import adapters

    return adapters


_DISCOVERY_REQUIRED_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")


class BlankFooterAdapter:
    def get_footer(self) -> FooterSettings:
        with _facade().SessionLocal() as db:
            row = db.get(_facade().PlatformSetting, "footer")
            if row is None:
                return _facade().FooterSettings(
                    footer_html_zh="企业应用 · © {year}", footer_html_en="Enterprise App · © {year}"
                )
            return _facade().FooterSettings.model_validate(row.value)

    def save_footer(self, footer: FooterSettings, *, actor_id: str) -> FooterSettings:
        safe = _facade().FooterSettings(
            footer_html_zh=_facade().sanitize_footer_html(footer.footer_html_zh),
            footer_html_en=_facade().sanitize_footer_html(footer.footer_html_en),
        )
        with _facade().SessionLocal() as db:
            row = db.get(_facade().PlatformSetting, "footer") or _facade().PlatformSetting(key="footer")
            before = dict(row.value) if row.value else None
            row.value = safe.model_dump(mode="json")
            db.add(row)
            db.add(
                _facade().PlatformAuditLog(
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
        with _facade().SessionLocal() as db:
            query = db.query(_facade().Notification).filter(_facade().Notification.account_id == user_id)
            if cursor:
                created_at, notification_id = _facade()._decode_notification_cursor(cursor)
                query = query.filter(
                    _facade().or_(
                        _facade().Notification.created_at < created_at,
                        _facade().and_(
                            _facade().Notification.created_at == created_at, _facade().Notification.id < notification_id
                        ),
                    )
                )
            rows = (
                query.order_by(_facade().Notification.created_at.desc(), _facade().Notification.id.desc())
                .limit(limit + 1)
                .all()
            )
            items = [
                _facade().NotificationItem(
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
                db.query(_facade().Notification.id)
                .filter(_facade().Notification.account_id == user_id, _facade().Notification.read_at.is_(None))
                .count()
            )
            return _facade().NotificationPage(
                items=items,
                unread_count=unread,
                next_cursor=_facade()._encode_notification_cursor(rows[limit - 1]) if len(rows) > limit else None,
            )

    def mark_read(self, user_id: str, notification_id: str, *, read_at: datetime) -> bool:
        with _facade().SessionLocal() as db:
            row = (
                db.query(_facade().Notification)
                .filter(_facade().Notification.account_id == user_id, _facade().Notification.id == notification_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.read_at = read_at
            db.commit()
            return True

    def mark_all_read(self, user_id: str, *, read_at: datetime) -> int:
        with _facade().SessionLocal() as db:
            count = (
                db.query(_facade().Notification)
                .filter(_facade().Notification.account_id == user_id, _facade().Notification.read_at.is_(None))
                .update({_facade().Notification.read_at: read_at})
            )
            db.commit()
            return count


def _encode_notification_cursor(row: Notification) -> str:
    payload = f"{_facade()._as_utc(row.created_at).isoformat()}|{row.id}".encode()
    return _facade().base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_notification_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = _facade().base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        created_at_text, notification_id_text = decoded.rsplit("|", 1)
        created_at = _facade().datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return created_at.astimezone(_facade().UTC), _facade().uuid.UUID(notification_id_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _facade().AuthError(400, "通知游标无效") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=_facade().UTC)


class BlankIntegrationAdapter:
    def get_oidc_settings(self) -> OidcSettings:
        data = _facade()._get_setting("oidc")
        return _facade().OidcSettings.model_validate(data)

    def save_oidc_settings(self, payload: OidcSettingsUpdate, *, actor_id: str) -> OidcSettings:
        payload = _facade().normalize_oidc_settings(payload)
        old = _facade()._get_setting("oidc")
        old_client_authority = _facade().oidc_client_authority(old)
        new_client_authority = _facade().oidc_client_authority(payload)
        client_authority_changed = old_client_authority.has_values() and old_client_authority != new_client_authority
        client_secret = payload.client_secret
        if client_authority_changed and not client_secret:
            raise _facade().AuthError(422, "身份提供方 authority 或 clientId 变更时必须重新填写 clientSecret")
        if client_secret is None:
            client_secret = _facade()._decrypt_saved_secret(old.get("client_secret"))
        data = payload.model_dump(mode="json", exclude={"client_secret"})
        data["has_client_secret"] = bool(client_secret)
        data["client_secret"] = _facade().encrypt_secret(client_secret or "")
        data["redirect_uri"] = (
            f"{payload.redirect_base_url.rstrip('/')}/api/v1/auth/oidc/callback" if payload.redirect_base_url else ""
        )
        _facade()._save_setting("oidc", data, actor_id=actor_id, action="identity.settings.update")
        return _facade().OidcSettings.model_validate(data)

    def test_oidc(self) -> ConnectionTestResult:
        configured = self.get_oidc_settings()
        server_base_url = configured.server_base_url.strip()
        return _facade().probe_jwks(
            _facade().rewrite_for_server_side(server_base_url, configured.jwks_uri),
            allow_localhost=_facade()._allow_local_outbound(),
            transport=(
                _facade().create_trusted_authority_transport(server_base_url)
                if server_base_url
                else _facade().guarded_request
            ),
        )

    @staticmethod
    def _discovery_response(transport, url: str):
        """返回 (response, 失败响应);两者恰有一个非 None。"""

        try:
            response = transport("GET", url, timeout=5, allow_localhost=_facade()._allow_local_outbound())
        except _facade().UnsafeOutboundUrlError as exc:
            return None, _facade().IdentityDiscoveryResponse(ok=False, error_kind="blocked", error_detail=str(exc))
        except _facade().httpx.HTTPError as exc:
            return None, _facade().IdentityDiscoveryResponse(
                ok=False, error_kind="unreachable", error_detail=str(exc)[:300]
            )
        if response.status_code != 200:
            return None, _facade().IdentityDiscoveryResponse(
                ok=False, error_kind="http_error", error_detail=f"discovery 返回 {response.status_code}"
            )
        return response, None

    @staticmethod
    def _discovery_payload_failure(payload):
        """载荷校验通过返回 None,否则返回失败响应。"""

        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str) for key in _DISCOVERY_REQUIRED_ENDPOINTS
        ):
            return _facade().IdentityDiscoveryResponse(
                ok=False, error_kind="invalid_response", error_detail="discovery 缺少必要端点"
            )
        try:
            for key in _DISCOVERY_REQUIRED_ENDPOINTS:
                from enterprise_platform.urls import validate_endpoint_url

                validate_endpoint_url(str(payload[key]))
        except ValueError as exc:
            return _facade().IdentityDiscoveryResponse(ok=False, error_kind="invalid_response", error_detail=str(exc))
        return None

    def discover_oidc(self, issuer: str | None) -> IdentityDiscoveryResponse:
        configured = self.get_oidc_settings()
        target = (issuer or configured.issuer).strip().rstrip("/")
        if not target:
            return _facade().IdentityDiscoveryResponse(
                ok=False, error_kind="not_configured", error_detail="issuer 不能为空"
            )
        server_base_url = configured.server_base_url.strip()
        url = _facade().rewrite_for_server_side(server_base_url, f"{target}/.well-known/openid-configuration")
        transport = (
            _facade().create_trusted_authority_transport(server_base_url)
            if server_base_url
            else _facade().guarded_request
        )
        response, failure = self._discovery_response(transport, url)
        if failure is not None:
            return failure
        try:
            payload = response.json()
        except ValueError:
            return _facade().IdentityDiscoveryResponse(
                ok=False, error_kind="invalid_response", error_detail="响应不是 JSON"
            )
        failure = self._discovery_payload_failure(payload)
        if failure is not None:
            return failure
        return _facade().IdentityDiscoveryResponse(
            ok=True,
            issuer=str(payload.get("issuer") or target),
            authorization_endpoint=str(payload["authorization_endpoint"]),
            token_endpoint=str(payload["token_endpoint"]),
            jwks_uri=str(payload["jwks_uri"]),
            userinfo_endpoint=str(payload.get("userinfo_endpoint") or ""),
        )

    def get_easyauth_status(self) -> EasyAuthStatus:
        data = _facade()._get_setting("easyauth")
        return _facade().EasyAuthStatus.model_validate(data or {})

    def save_easyauth_settings(self, payload: EasyAuthSettingsUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _facade()._get_setting("easyauth")
        authority_changed = bool(old.get("base_url") or old.get("app_key")) and (
            old.get("base_url", "").rstrip("/") != payload.base_url.rstrip("/")
            or old.get("app_key", "") != payload.app_key
        )
        credential = payload.credential
        if authority_changed and not credential:
            raise _facade().AuthError(422, "EasyAuth authority 或 appKey 变更时必须重新填写 credential")
        if credential is None:
            credential = _facade()._decrypt_saved_secret(old.get("credential"))
        credential = credential or ""
        data = {
            "configured": bool(payload.base_url and payload.app_key and credential),
            "base_url": payload.base_url.rstrip("/"),
            "app_key": payload.app_key,
            "auth_mode": "static_app_token",
            "has_credential": bool(credential),
            "permission_request_url": payload.permission_request_url,
            "credential": _facade().encrypt_secret(credential),
        }
        _facade()._save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return _facade().EasyAuthStatus.model_validate(data)

    def save_permission_request_url(self, payload: PermissionRequestUrlUpdate, *, actor_id: str) -> EasyAuthStatus:
        old = _facade()._get_setting("easyauth")
        data = dict(old)
        data["permission_request_url"] = payload.permission_request_url.strip()
        _facade()._save_setting("easyauth", data, actor_id=actor_id, action="authz.settings.update")
        return _facade().EasyAuthStatus.model_validate(data)

    def test_easyauth(self) -> ConnectionTestResult:
        data = _facade()._get_setting("easyauth")
        try:
            credential = _facade().decrypt_secret(str(data.get("credential") or ""))
        except _facade().SecretConfigurationError as exc:
            return _facade().ConnectionTestResult(ok=False, error_kind="configuration", error_detail=str(exc))
        client = _facade().EasyAuthPermissionClient(
            base_url=str(data.get("base_url") or ""),
            app_key=str(data.get("app_key") or "enterprise-blank"),
            auth_mode=str(data.get("auth_mode") or "static_app_token"),
            credential=credential,
            timeout=5,
        )
        try:
            snapshot = client.fetch_permission_snapshot("enterprise-platform-connectivity-probe")
        except _facade().EasyAuthForbiddenError as exc:
            kind = _facade().classify_connection_failure(str(exc), forbidden=True)
            return _facade().ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        except _facade().EasyAuthClientError as exc:
            kind = _facade().classify_connection_failure(
                str(exc),
                network_error=isinstance(exc.__cause__, _facade().httpx.RequestError),
            )
            return _facade().ConnectionTestResult(ok=False, error_kind=kind.value, error_detail=str(exc))
        finally:
            client.close()
        return _facade().ConnectionTestResult(ok=True, error_detail=f"snapshot {snapshot.snapshot_version}")


class BlankDirectoryAdapter:
    """blank 宿主持久化目录设置，但不投影用户、不探测 EasyAuth 目录。

    test/sync 固定返回 ``not_configured``：本宿主没有用户目录投影，也尚未接入
    DirectoryClient（由业务宿主实现）。设置写入 ``platform_settings.directory``。
    """

    def get_directory_settings(self) -> DirectorySettings:
        return self._settings_from_stored(_facade()._get_setting("directory"))

    def save_directory_settings(self, payload: DirectorySettingsUpdate, *, actor_id: str) -> DirectorySettings:
        old = _facade()._get_setting("directory")
        identity_changed = bool(old.get("base_url") or old.get("app_key") or old.get("auth_mode")) and (
            _facade().normalize_base_url(str(old.get("base_url") or ""))
            != _facade().normalize_base_url(payload.base_url)
            or str(old.get("app_key") or "") != payload.app_key
            or str(old.get("auth_mode") or "") != payload.auth_mode
        )
        credential = payload.credential
        if identity_changed and not credential:
            raise _facade().AuthError(422, "目录服务 authority、appKey 或 authMode 变更时必须重新填写 credential")
        if credential is None:
            credential = _facade()._decrypt_saved_secret(old.get("credential"))
        credential = credential or ""
        data = {
            "enabled": payload.enabled,
            "base_url": _facade().normalize_base_url(payload.base_url),
            "app_key": payload.app_key.strip(),
            "auth_mode": payload.auth_mode,
            "sync_interval_minutes": payload.sync_interval_minutes,
            "has_credential": bool(credential),
            "credential": _facade().encrypt_secret(credential),
            "last_sync": old.get("last_sync"),
        }
        _facade()._save_setting("directory", data, actor_id=actor_id, action="identity.directory.update")
        return self._settings_from_stored(data)

    def test_directory(self) -> ConnectionTestResult:
        return _facade().ConnectionTestResult(
            ok=False,
            error_kind="not_configured",
            error_detail="blank 宿主不探测 EasyAuth 目录连接",
        )

    def sync_directory(self, *, actor_id: str) -> DirectorySyncResult:
        result = _facade().DirectorySyncResult(
            status="not_configured",
            at=_facade().datetime.now(_facade().UTC),
            summary="blank 宿主不投影目录用户，目录同步未配置。",
        )
        stored = _facade()._get_setting("directory")
        stored["last_sync"] = result.model_dump(mode="json")
        _facade()._save_setting("directory", stored, actor_id=actor_id, action="identity.directory.sync")
        return result

    @staticmethod
    def _settings_from_stored(data: dict) -> DirectorySettings:
        payload = dict(data)
        payload.pop("credential", None)
        raw_last_sync = payload.get("last_sync")
        last_sync = _facade().DirectorySyncResult.model_validate(raw_last_sync) if raw_last_sync else None
        payload["last_sync"] = last_sync
        return _facade().DirectorySettings.model_validate(
            {
                "enabled": False,
                "base_url": "",
                "app_key": "",
                "has_credential": False,
                "auth_mode": "static_app_token",
                "sync_interval_minutes": 30,
                "last_sync": None,
                **payload,
            }
        )


class BlankUpstreamHealthAdapter:
    probes = {
        "authentik": ("Authentik(SSO 登录)", True, lambda: _facade().BlankIntegrationAdapter().test_oidc()),
        "authentik_directory": ("Authentik 用户同步", False, None),
        "easyauth": ("EasyAuth(权限授权)", True, lambda: _facade().BlankIntegrationAdapter().test_easyauth()),
        "scheduler": ("定时任务调度器", False, None),
    }

    def latest(self) -> list[UpstreamHealthItem]:
        with _facade().SessionLocal() as db:
            result: list[UpstreamHealthItem] = []
            for dependency, (display_name, supported, _) in self.probes.items():
                row = (
                    db.query(_facade().HealthSnapshot)
                    .filter(_facade().HealthSnapshot.dependency == dependency)
                    .order_by(_facade().HealthSnapshot.checked_at.desc())
                    .first()
                )
                result.append(
                    _facade().UpstreamHealthItem(
                        dependency=dependency,
                        display_name=display_name,
                        status=row.status if row else "unknown",
                        checked_at=row.checked_at if row else None,
                        summary=_facade().safe_health_summary(row.summary) if row else "尚未记录健康快照",
                        error_summary=_facade().safe_health_summary(row.error_summary) if row else "",
                        summary_code=(
                            _facade()._health_summary_code(row.status, row.summary) if row else "upstream.not_checked"
                        )
                        if supported
                        else "upstream.not_supported",
                        supported=supported,
                    )
                )
            return result

    def run_checks(self, *, actor_id: str) -> list[UpstreamHealthItem]:
        with _facade().SessionLocal() as db:
            for dependency, (display_name, supported, probe) in self.probes.items():
                if not supported or probe is None:
                    result = _facade().ConnectionTestResult(
                        ok=False, error_kind="not_supported", error_detail="capability is not supported by this host"
                    )
                else:
                    result = probe()
                db.add(
                    _facade().HealthSnapshot(
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
