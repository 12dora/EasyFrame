"""blank platform core tables."""

import sqlalchemy as sa

from alembic import op

revision = "0001_platform_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("email", sa.String(200)),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("password_hash", sa.String(200)),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_user_id", sa.String(128)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ui_locale", sa.String(10), nullable=False, server_default="zh-CN"),
        sa.Column("totp_secret", sa.String(100)),
        sa.Column("totp_pending_secret", sa.String(100)),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sessions_revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_platform_accounts_username"),
        sa.UniqueConstraint("external_source", "external_user_id", name="uq_platform_account_external_identity"),
    )
    op.create_index("ix_platform_accounts_username", "platform_accounts", ["username"])
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "platform_passkeys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False, unique=True),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_platform_passkeys_account_id", "platform_passkeys", ["account_id"])
    op.create_table(
        "platform_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("level", sa.String(20), nullable=False, server_default="info"),
        sa.Column("href", sa.Text()),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_notifications_account_id", "platform_notifications", ["account_id"])
    op.create_index("ix_platform_notifications_created_at", "platform_notifications", ["created_at"])
    op.create_table(
        "platform_health_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dependency", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("error_summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_health_dependency", "platform_health_snapshots", ["dependency"])
    op.create_index("ix_platform_health_checked_at", "platform_health_snapshots", ["checked_at"])
    op.create_table(
        "platform_permission_catalog",
        sa.Column("code", sa.String(160), primary_key=True),
        sa.Column("name_zh", sa.String(200), nullable=False),
        sa.Column("name_en", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(80), nullable=False),
        sa.Column("resource", sa.String(120), nullable=False),
        sa.Column("supported_scopes", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="standard"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_table(
        "platform_permission_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("external_source", sa.String(64), nullable=False),
        sa.Column("external_user_id", sa.String(128), nullable=False),
        sa.Column("app_key", sa.String(160), nullable=False),
        sa.Column("groups", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("grants", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("grant_version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_version", sa.String(160), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_source", "external_user_id", "app_key", name="uq_platform_permission_snapshot"),
    )
    op.create_index("ix_platform_permission_snapshots_account_id", "platform_permission_snapshots", ["account_id"])


def downgrade() -> None:
    op.drop_table("platform_permission_snapshots")
    op.drop_table("platform_permission_catalog")
    op.drop_table("platform_health_snapshots")
    op.drop_table("platform_notifications")
    op.drop_table("platform_passkeys")
    op.drop_table("platform_settings")
    op.drop_table("platform_accounts")
