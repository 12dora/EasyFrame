"""local-accounts v2 迁移与数据库不变量。"""

from __future__ import annotations

import importlib
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from blank_app.database import SessionLocal, engine
from blank_app.models import Account, PermissionCatalog

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def _migration_module():
    return importlib.import_module("blank_app.alembic.versions.0004_local_accounts_v2")


def test_model_metadata_matches_local_accounts_v2_schema() -> None:
    account_columns = Account.__table__.c
    assert account_columns.local_grants_version.nullable is False
    assert str(account_columns.local_grants_version.server_default.arg) == "0"
    assert account_columns.expires_at.nullable is True
    assert account_columns.expires_at.type.timezone is True
    assert "ck_platform_accounts_external_identity" in {constraint.name for constraint in Account.__table__.constraints}

    catalog_columns = PermissionCatalog.__table__.c
    assert catalog_columns.group_key.nullable is True
    assert "ck_platform_permission_catalog_risk_level" in {
        constraint.name for constraint in PermissionCatalog.__table__.constraints
    }


def test_local_permissions_data_upgrade_and_downgrade_are_idempotent() -> None:
    migration = _migration_module()
    v1 = ["all.code", "self.code", "missing.code"]
    scopes = {
        "all.code": ["SELF", "ALL"],
        "self.code": ["SELF"],
    }
    upgraded = migration.upgrade_local_permissions(v1, scopes)
    assert upgraded == [
        {"code": "all.code", "scope": "ALL"},
        {"code": "self.code", "scope": "SELF"},
        {"code": "missing.code", "scope": "ALL"},
    ]
    assert migration.upgrade_local_permissions(upgraded, scopes) == upgraded
    assert migration.downgrade_local_permissions(upgraded) == v1
    assert migration.downgrade_local_permissions(v1) == v1


def test_upgrade_normalizes_legacy_risk_and_local_permissions() -> None:
    config = Config("blank_app/alembic.ini")
    account_id = uuid.uuid4()
    username = f"migration-v1-{uuid.uuid4().hex}"
    permission_code = f"test.migration-v1.{uuid.uuid4().hex}"
    catalog = sa.table(
        "platform_permission_catalog",
        sa.column("code", sa.String()),
        sa.column("name_zh", sa.String()),
        sa.column("name_en", sa.String()),
        sa.column("domain", sa.String()),
        sa.column("resource", sa.String()),
        sa.column("supported_scopes", sa.JSON()),
        sa.column("risk_level", sa.String()),
    )
    accounts = sa.table(
        "platform_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("username", sa.String()),
        sa.column("local_permissions", sa.JSON()),
    )

    try:
        engine.dispose()
        command.downgrade(config, "0003_local_permissions")
        with engine.begin() as connection:
            connection.execute(
                sa.insert(catalog).values(
                    code=permission_code,
                    name_zh=permission_code,
                    name_en=permission_code,
                    domain="test",
                    resource="test.migration-v1",
                    supported_scopes=["SELF", "ALL"],
                    risk_level="medium",
                )
            )
            connection.execute(
                sa.insert(accounts).values(
                    id=account_id,
                    username=username,
                    local_permissions=[permission_code],
                )
            )

        engine.dispose()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(sa.select(catalog.c.risk_level).where(catalog.c.code == permission_code)) == "high"
            assert connection.scalar(sa.select(accounts.c.local_permissions).where(accounts.c.id == account_id)) == [
                {"code": permission_code, "scope": "ALL"}
            ]
    finally:
        try:
            engine.dispose()
            with engine.begin() as connection:
                connection.execute(sa.delete(accounts).where(accounts.c.id == account_id))
                connection.execute(sa.delete(catalog).where(catalog.c.code == permission_code))
        finally:
            engine.dispose()
            command.upgrade(config, "head")
            engine.dispose()


@pytest.mark.parametrize(
    ("password_hash", "is_admin"),
    [("unexpected-password-hash", False), (None, True)],
)
def test_external_identity_check_rejects_password_or_admin(password_hash: str | None, is_admin: bool) -> None:
    with SessionLocal() as db:
        db.add(
            Account(
                username=f"invalid-external-{uuid.uuid4().hex}",
                password_hash=password_hash,
                external_source="authentik",
                external_user_id=uuid.uuid4().hex,
                is_admin=is_admin,
                must_change_password=False,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_catalog_risk_level_check_rejects_unknown_value() -> None:
    code = f"test.invalid-risk.{uuid.uuid4().hex}"
    with SessionLocal() as db:
        db.add(
            PermissionCatalog(
                code=code,
                name_zh=code,
                name_en=code,
                domain="test",
                resource="test.invalid-risk",
                supported_scopes=["ALL"],
                risk_level="medium",
                active=True,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
