"""add ui_preferences json to platform accounts."""

import sqlalchemy as sa
from alembic import op

revision = "0006_ui_preferences"
down_revision = "0005_oidc_id_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_accounts",
        sa.Column("ui_preferences", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("platform_accounts", "ui_preferences")
