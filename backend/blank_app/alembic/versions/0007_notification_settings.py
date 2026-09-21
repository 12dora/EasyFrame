"""notification settings policy and preference tables."""

import sqlalchemy as sa
from alembic import op

revision = "0007_notification_settings"
down_revision = "0006_ui_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_notification_policies",
        sa.Column("group_key", sa.String(80), primary_key=True),
        sa.Column("managed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("switches", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(100)),
    )
    op.create_table(
        "platform_notification_preferences",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("platform_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("group_key", sa.String(80), primary_key=True),
        sa.Column("switches", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("platform_notification_preferences")
    op.drop_table("platform_notification_policies")
