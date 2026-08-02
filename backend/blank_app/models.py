"""blank host 独立最小模型；迁移 owner 为 ``blank_app/alembic``。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from blank_app.database import BlankBase


class Account(BlankBase):
    __tablename__ = "platform_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(String(200))
    external_source: Mapped[str | None] = mapped_column(String(64))
    external_user_id: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    local_permissions: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    ui_locale: Mapped[str] = mapped_column(String(10), default="zh-CN", server_default="zh-CN")
    totp_secret: Mapped[str | None] = mapped_column(String(100))
    totp_pending_secret: Mapped[str | None] = mapped_column(String(100))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    sessions_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("external_source", "external_user_id", name="uq_platform_account_external_identity"),
    )


class Passkey(BlankBase):
    __tablename__ = "platform_passkeys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("platform_accounts.id"), index=True)
    credential_id: Mapped[str] = mapped_column(Text, unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformSetting(BlankBase):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlatformAuditLog(BlankBase):
    __tablename__ = "platform_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    before_data: Mapped[dict | None] = mapped_column(JSON)
    after_data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DescriptorKey(BlankBase):
    __tablename__ = "platform_descriptor_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    token_prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(BlankBase):
    __tablename__ = "platform_notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("platform_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    level: Mapped[str] = mapped_column(String(20), default="info", server_default="info")
    href: Mapped[str | None] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class HealthSnapshot(BlankBase):
    __tablename__ = "platform_health_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dependency: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(String(500), default="", server_default="")
    error_summary: Mapped[str] = mapped_column(String(500), default="", server_default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PermissionCatalog(BlankBase):
    __tablename__ = "platform_permission_catalog"

    code: Mapped[str] = mapped_column(String(160), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str] = mapped_column(String(120))
    supported_scopes: Mapped[list[str]] = mapped_column(JSON)
    risk_level: Mapped[str] = mapped_column(String(30), default="standard", server_default="standard")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class PermissionSnapshot(BlankBase):
    __tablename__ = "platform_permission_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("platform_accounts.id"), index=True)
    external_source: Mapped[str] = mapped_column(String(64))
    external_user_id: Mapped[str] = mapped_column(String(128))
    app_key: Mapped[str] = mapped_column(String(160))
    groups: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    grants: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    grant_version: Mapped[int] = mapped_column(Integer)
    catalog_version: Mapped[int] = mapped_column(Integer)
    snapshot_version: Mapped[str] = mapped_column(String(160))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("external_source", "external_user_id", "app_key", name="uq_platform_permission_snapshot"),
    )
