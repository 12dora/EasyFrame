"""lifespan 种子:多 worker 并发 INSERT 必须幂等。"""

from __future__ import annotations

import threading
import uuid

import pytest

from blank_app.database import SessionLocal
from blank_app.models import Account, PermissionCatalog
from blank_app.permission_registry import FRAMEWORK_PERMISSIONS

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def test_concurrent_seed_platform_catalog_is_idempotent() -> None:
    from blank_app.authz_snapshot import seed_platform_catalog

    registered = FRAMEWORK_PERMISSIONS[0]
    with SessionLocal() as db:
        row = db.get(PermissionCatalog, registered.code)
        assert row is not None
        db.delete(row)
        db.commit()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            seed_platform_catalog()
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive()
    assert errors == []
    with SessionLocal() as db:
        assert db.get(PermissionCatalog, registered.code) is not None


def test_concurrent_seed_default_admin_is_idempotent(monkeypatch) -> None:
    from blank_app.adapter_support import seed_default_admin

    username = f"seed-race-{uuid.uuid4().hex}"
    monkeypatch.setenv("BLANK_LOCAL_AUTH_MODE", "development")
    monkeypatch.setenv("BLANK_RUNTIME_ENV", "test")
    monkeypatch.setenv("BLANK_ADMIN_USERNAME", username)
    monkeypatch.setenv("BLANK_ADMIN_PASSWORD", "Concurrent-bootstrap-password!")
    errors: list[BaseException] = []

    def run() -> None:
        try:
            seed_default_admin()
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert not worker.is_alive()
    try:
        assert errors == []
        with SessionLocal() as db:
            rows = db.query(Account).filter(Account.username == username).all()
            assert len(rows) == 1
            assert rows[0].is_admin is True
    finally:
        with SessionLocal() as db:
            db.query(Account).filter(Account.username == username).delete()
            db.commit()
