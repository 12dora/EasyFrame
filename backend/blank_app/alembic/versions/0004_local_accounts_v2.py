"""add local-account v2 grant and identity fields."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "0004_local_accounts_v2"
down_revision = "0003_local_permissions"
branch_labels = None
depends_on = None


def _preferred_local_scope(scopes: object) -> str:
    """本地授权只在 ALL 与 SELF 中取最大可用 scope。"""

    if isinstance(scopes, Sequence) and not isinstance(scopes, str | bytes):
        if "ALL" in scopes:
            return "ALL"
        if "SELF" in scopes:
            return "SELF"
    # 目录缺失或没有本地可用 scope 时保留授权，交由读取归一化剔除。
    return "ALL"


def upgrade_local_permissions(value: object, catalog_scopes: Mapping[str, object]) -> object:
    """把 v1 code 字符串升级为 v2 grant；已有对象保持不变。"""

    if not isinstance(value, list):
        return value
    return [
        {"code": item, "scope": _preferred_local_scope(catalog_scopes.get(item))}
        if isinstance(item, str)
        else item
        for item in value
    ]


def downgrade_local_permissions(value: object) -> object:
    """把有效 v2 grant 对象还原为 v1 code 字符串。"""

    if not isinstance(value, list):
        return value
    return [
        item["code"]
        if isinstance(item, dict) and isinstance(item.get("code"), str)
        else item
        for item in value
    ]


def _transform_local_permissions(*, downgrade: bool = False) -> None:
    accounts = sa.table(
        "platform_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("local_permissions", sa.JSON()),
    )
    catalog = sa.table(
        "platform_permission_catalog",
        sa.column("code", sa.String()),
        sa.column("supported_scopes", sa.JSON()),
    )
    connection = op.get_bind()
    catalog_scopes = dict(
        connection.execute(sa.select(catalog.c.code, catalog.c.supported_scopes)).all()
    )
    for account_id, permissions in connection.execute(
        sa.select(accounts.c.id, accounts.c.local_permissions)
    ):
        transformed: Any = (
            downgrade_local_permissions(permissions)
            if downgrade
            else upgrade_local_permissions(permissions, catalog_scopes)
        )
        if transformed != permissions:
            connection.execute(
                sa.update(accounts)
                .where(accounts.c.id == account_id)
                .values(local_permissions=transformed)
            )


def upgrade() -> None:
    op.add_column(
        "platform_accounts",
        sa.Column("local_grants_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "platform_accounts",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_platform_accounts_external_identity",
        "platform_accounts",
        "external_source IS NULL OR (password_hash IS NULL AND is_admin = false)",
    )
    op.add_column(
        "platform_permission_catalog",
        sa.Column("group_key", sa.Text(), nullable=True),
    )

    # 约束建立前先把未知存量风险等级按高风险收紧。
    catalog = sa.table(
        "platform_permission_catalog",
        sa.column("risk_level", sa.String()),
    )
    op.execute(
        sa.update(catalog)
        .where(catalog.c.risk_level.notin_(("standard", "high")))
        .values(risk_level="high")
    )
    op.create_check_constraint(
        "ck_platform_permission_catalog_risk_level",
        "platform_permission_catalog",
        "risk_level IN ('standard', 'high')",
    )
    _transform_local_permissions()


def downgrade() -> None:
    _transform_local_permissions(downgrade=True)
    op.drop_constraint(
        "ck_platform_permission_catalog_risk_level",
        "platform_permission_catalog",
        type_="check",
    )
    op.drop_column("platform_permission_catalog", "group_key")
    op.drop_constraint(
        "ck_platform_accounts_external_identity",
        "platform_accounts",
        type_="check",
    )
    op.drop_column("platform_accounts", "expires_at")
    op.drop_column("platform_accounts", "local_grants_version")
