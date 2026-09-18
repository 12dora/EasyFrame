"""blank_app ui_preferences column, migration, and session persistence."""

from __future__ import annotations

import importlib
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from blank_app.database import SessionLocal, engine
from blank_app.models import Account
from platform_tests.test_blank_app_api import _login, _reset_admin

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def test_model_metadata_matches_ui_preferences_schema() -> None:
    column = Account.__table__.c.ui_preferences
    assert column.nullable is False
    assert str(column.server_default.arg) == "{}"


def test_ui_preferences_migration_follows_oidc_id_token() -> None:
    module = importlib.import_module("blank_app.alembic.versions.0006_ui_preferences")
    assert module.revision == "0006_ui_preferences"
    assert module.down_revision == "0005_oidc_id_token"


def test_ui_preferences_upgrade_adds_empty_object_default() -> None:
    config = Config("blank_app/alembic.ini")
    account_id = uuid.uuid4()
    username = f"prefs-{uuid.uuid4().hex}"
    accounts = sa.table(
        "platform_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("username", sa.String()),
        sa.column("ui_preferences", sa.JSON()),
    )
    try:
        engine.dispose()
        command.downgrade(config, "0005_oidc_id_token")
        assert "ui_preferences" not in _account_columns()
        with engine.begin() as connection:
            connection.execute(sa.insert(accounts).values(id=account_id, username=username))
        engine.dispose()
        command.upgrade(config, "head")
        assert "ui_preferences" in _account_columns()
        with engine.connect() as connection:
            value = connection.scalar(sa.select(accounts.c.ui_preferences).where(accounts.c.id == account_id))
        assert value == {}
    finally:
        engine.dispose()
        with engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id == account_id))
        engine.dispose()
        command.upgrade(config, "head")
        engine.dispose()


def test_blank_session_preferences_persist_on_account() -> None:
    from blank_app.main import app

    _reset_admin()
    with TestClient(app) as client:
        login = _login(client)
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        try:
            session = client.get("/api/v1/auth/session", headers=headers)
            assert session.status_code == 200
            assert session.json()["preferences"] == {"tableDensity": "compact"}
            patched = client.patch("/api/v1/auth/preferences", headers=headers, json={"tableDensity": "comfortable"})
            assert patched.status_code == 200, patched.text
            assert patched.json()["preferences"] == {"tableDensity": "comfortable"}
            assert client.get("/api/v1/auth/session", headers=headers).json()["preferences"] == {
                "tableDensity": "comfortable"
            }
            with SessionLocal() as db:
                account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
                assert account.ui_preferences.get("table_density") == "comfortable"
        finally:
            with SessionLocal() as db:
                account = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
                account.ui_preferences = {}
                db.commit()


def _account_columns() -> set[str]:
    with engine.connect() as connection:
        return {column["name"] for column in inspect(connection).get_columns("platform_accounts")}
