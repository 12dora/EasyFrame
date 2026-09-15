"""persist oidc id_token for RP-initiated logout."""

import sqlalchemy as sa
from alembic import op

revision = "0005_oidc_id_token"
down_revision = "0004_local_accounts_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("platform_accounts", sa.Column("oidc_id_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_accounts", "oidc_id_token")
