"""blank host 权限注册表防漂移约束。"""

from __future__ import annotations

import uuid

import pytest

from blank_app.database import SessionLocal
from blank_app.models import PermissionCatalog
from blank_app.permission_registry import FRAMEWORK_PERMISSIONS
from enterprise_platform.authz import normalize_catalog_risk_level
from enterprise_platform.local_accounts import BASELINE_SELF_SERVICE

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")


def _registry_codes() -> set[str]:
    return {permission.code for permission in FRAMEWORK_PERMISSIONS}


def test_superadmin_permissions_are_derived_from_registry() -> None:
    from blank_app.adapters import ALL_PERMISSIONS
    from blank_app.authz_api import FRAMEWORK_PERMISSIONS as AUTHZ_FRAMEWORK_PERMISSIONS

    assert AUTHZ_FRAMEWORK_PERMISSIONS is FRAMEWORK_PERMISSIONS
    assert ALL_PERMISSIONS == _registry_codes()


def test_seed_platform_catalog_upserts_registry_and_deactivates_other_codes() -> None:
    from blank_app.authz_api import seed_platform_catalog

    outside_code = f"test.outside.{uuid.uuid4().hex}"
    registered = FRAMEWORK_PERMISSIONS[0]
    with SessionLocal() as db:
        row = db.get(PermissionCatalog, registered.code)
        assert row is not None
        row.domain = "drifted"
        row.group_key = "drifted.group"
        row.risk_level = "standard" if registered.risk_level == "high" else "high"
        row.active = False
        db.add(
            PermissionCatalog(
                code=outside_code,
                name_zh=outside_code,
                name_en=outside_code,
                domain="test",
                resource="test.outside",
                supported_scopes=["ALL"],
                risk_level="standard",
                active=True,
            )
        )
        db.commit()

    seed_platform_catalog()

    try:
        with SessionLocal() as db:
            rows = {row.code: row for row in db.query(PermissionCatalog).all()}
            assert {code for code, row in rows.items() if row.active} == {
                permission.code for permission in FRAMEWORK_PERMISSIONS if permission.active
            }
            for permission in FRAMEWORK_PERMISSIONS:
                row = rows[permission.code]
                assert row.domain == permission.domain
                assert row.resource == permission.resource
                assert row.group_key == permission.group_key
                assert row.supported_scopes == [scope.value for scope in permission.supported_scopes]
                assert row.risk_level == permission.risk_level
                assert row.active is permission.active
            assert rows[outside_code].active is False
    finally:
        with SessionLocal() as db:
            db.query(PermissionCatalog).filter(PermissionCatalog.code == outside_code).delete()
            db.commit()


def test_seed_platform_catalog_inserts_missing_registry_row() -> None:
    from blank_app.authz_api import seed_platform_catalog

    registered = FRAMEWORK_PERMISSIONS[0]
    with SessionLocal() as db:
        previous = db.get(PermissionCatalog, registered.code)
        assert previous is not None
        expected_names = (previous.name_zh, previous.name_en)
        db.delete(previous)
        db.commit()

    seed_platform_catalog()

    with SessionLocal() as db:
        row = db.get(PermissionCatalog, registered.code)
        assert row is not None
        assert (row.name_zh, row.name_en) == expected_names
        assert row.group_key == registered.group_key
        assert row.supported_scopes == [scope.value for scope in registered.supported_scopes]
        assert row.risk_level == registered.risk_level
        assert row.active is registered.active


def test_seed_platform_catalog_persists_optional_group_key(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    original = authz_api.FRAMEWORK_PERMISSIONS
    target = original[0]
    grouped = target.model_copy(update={"group_key": "auth.self_service"})
    monkeypatch.setattr(
        authz_api,
        "FRAMEWORK_PERMISSIONS",
        tuple(grouped if permission.code == target.code else permission for permission in original),
    )
    try:
        authz_api.seed_platform_catalog()
        with SessionLocal() as db:
            row = db.get(PermissionCatalog, target.code)
            assert row is not None
            assert row.group_key == "auth.self_service"
    finally:
        # 测试约束:恢复真实注册表投影，避免临时分组污染后续用例。
        monkeypatch.setattr(authz_api, "FRAMEWORK_PERMISSIONS", original)
        authz_api.seed_platform_catalog()


def test_manifest_keeps_v1_domain_group_projection(monkeypatch) -> None:
    import blank_app.authz_api as authz_api

    target = FRAMEWORK_PERMISSIONS[0]
    permissions = tuple(
        permission.model_copy(update={"group_key": "accounts.local"}) if permission.code == target.code else permission
        for permission in FRAMEWORK_PERMISSIONS
    )
    monkeypatch.setattr(
        authz_api,
        "FRAMEWORK_MANIFEST",
        authz_api.FRAMEWORK_MANIFEST.model_copy(update={"permissions": permissions}),
    )

    manifest = authz_api._current_manifest()
    declared_groups = {group["key"] for group in manifest["permission_groups"]}
    projected = next(item for item in manifest["permissions"] if item["key"] == target.code)

    # v1 约束:目录分组不得泄漏到 EasyAuth wire format。
    assert projected["group_key"] == target.domain
    assert all(item["group_key"] in declared_groups for item in manifest["permissions"])


def test_baseline_permissions_are_registered() -> None:
    assert BASELINE_SELF_SERVICE <= _registry_codes()


def test_registry_entries_have_supported_scopes_and_known_risk_levels() -> None:
    assert all(permission.supported_scopes for permission in FRAMEWORK_PERMISSIONS)
    assert all(permission.risk_level in {"standard", "high"} for permission in FRAMEWORK_PERMISSIONS)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("standard", "standard"), ("high", "high"), ("medium", "high"), (None, "high")],
)
def test_catalog_risk_level_normalization_fails_closed(stored: object, expected: str) -> None:
    assert normalize_catalog_risk_level(stored) == expected
