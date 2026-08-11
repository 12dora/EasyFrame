"""add local permissions to platform accounts."""

import sqlalchemy as sa
from alembic import op

revision = "0003_local_permissions"
down_revision = "0002_security_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_accounts",
        sa.Column("local_permissions", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("platform_accounts", "local_permissions")
