"""blank host 对共享 OIDC flow 的账号投影适配。"""

import os
from datetime import UTC, datetime

from blank_app.adapters import OIDC_STATE_KEY_PURPOSE, _get_setting, account_adapter, signing_key
from blank_app.database import SessionLocal
from blank_app.models import Account
from enterprise_platform.oidc import OidcConfig, OidcFlowError, OidcIdentity
from enterprise_platform.oidc_settings import rewrite_for_server_side
from enterprise_platform.safe_http import guarded_request
from enterprise_platform.secrets import SecretConfigurationError, decrypt_secret
from enterprise_platform.trusted_http import create_trusted_authority_transport


class BlankOidcHost:
    def config(self) -> OidcConfig:
        data = _get_setting("oidc")
        try:
            client_secret = decrypt_secret(str(data.get("client_secret") or ""))
        except SecretConfigurationError as exc:
            raise OidcFlowError(
                "OIDC credential storage is not configured", kind="not_configured", status_code=409
            ) from exc
        server_base_url = str(data.get("server_base_url") or "").strip()
        http_transport = create_trusted_authority_transport(server_base_url) if server_base_url else guarded_request
        return OidcConfig(
            enabled=bool(data.get("enabled")),
            issuer=str(data.get("issuer") or ""),
            authorization_endpoint=str(data.get("authorization_endpoint") or ""),
            token_endpoint=rewrite_for_server_side(server_base_url, str(data.get("token_endpoint") or "")),
            jwks_uri=rewrite_for_server_side(server_base_url, str(data.get("jwks_uri") or "")),
            userinfo_endpoint=rewrite_for_server_side(server_base_url, str(data.get("userinfo_endpoint") or "")),
            client_id=str(data.get("client_id") or ""),
            client_secret=client_secret,
            scopes=str(data.get("scopes") or "openid profile email"),
            redirect_uri=str(data.get("redirect_uri") or ""),
            frontend_base_url=str(data.get("frontend_base_url") or ""),
            # OIDC state 与 session 使用不同用途派生的密钥，互不通用。
            signing_secret=signing_key(OIDC_STATE_KEY_PURPOSE),
            allow_local_outbound=os.getenv("BLANK_ALLOW_LOCAL_OUTBOUND", "false").lower() in {"1", "true", "yes"},
            http_transport=http_transport,
        )

    def upsert_identity(self, identity: OidcIdentity) -> str:
        with SessionLocal() as db:
            account = (
                db.query(Account)
                .filter(Account.external_source == "authentik", Account.external_user_id == identity.sub)
                .one_or_none()
            )
            if account is not None and not account.active:
                raise OidcFlowError("该账号已停用", kind="inactive_user", status_code=403)
            if account is None:
                username = identity.name
                if db.query(Account.id).filter(Account.username == username).first() is not None:
                    username = f"{identity.name}-{identity.sub[:8]}"
                account = Account(
                    username=username,
                    email=identity.email or None,
                    avatar_url=identity.avatar_url,
                    password_hash=None,
                    external_source="authentik",
                    external_user_id=identity.sub,
                    active=True,
                    is_admin=False,
                    must_change_password=False,
                )
                db.add(account)
            account.email = identity.email or account.email
            account.avatar_url = identity.avatar_url
            db.commit()
            db.refresh(account)
            account_id = str(account.id)
        from blank_app.authz_api import ensure_account_snapshot

        ensure_account_snapshot(account_id, force=True)
        return account_id

    def issue_session(self, account_id: str) -> str:
        return account_adapter.issue_session(account_id)

    def revoke_sessions_by_subject(self, sub: str) -> int:
        with SessionLocal() as db:
            count = (
                db.query(Account)
                .filter(Account.external_source == "authentik", Account.external_user_id == sub)
                .update({Account.sessions_revoked_at: datetime.now(UTC)}, synchronize_session=False)
            )
            db.commit()
            return count
