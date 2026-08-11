"""security audit and descriptor-key persistence."""

import sqlalchemy as sa
from alembic import op

revision = "0002_security_audit"
down_revision = "0001_platform_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("before_data", sa.JSON()),
        sa.Column("after_data", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_audit_logs_actor_id", "platform_audit_logs", ["actor_id"])
    op.create_index("ix_platform_audit_logs_action", "platform_audit_logs", ["action"])
    op.create_index("ix_platform_audit_logs_created_at", "platform_audit_logs", ["created_at"])
    op.create_table(
        "platform_descriptor_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_descriptor_keys_active", "platform_descriptor_keys", ["active"])


def downgrade() -> None:
    op.drop_table("platform_descriptor_keys")
    op.drop_table("platform_audit_logs")
