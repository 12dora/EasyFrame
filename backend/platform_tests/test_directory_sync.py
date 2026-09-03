"""目录同步内核（无库）与 blank 宿主目录路由合同。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enterprise_platform.directory_sync import run_directory_sync
from enterprise_platform.easyauth.types import (
    DirectorySnapshotDriftError,
    DirectorySnapshotMeta,
    DirectorySnapshotRead,
    DirectoryUnavailableError,
    DirectoryUserRecord,
)

SYNC_AT = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)


def _user(**overrides) -> DirectoryUserRecord:
    payload = {
        "user_ref": "dt:v1:keep",
        "user_id": "ak-keep",
        "source_slug": "dingtalk",
        "corp_id": "corp-demo",
        "dingtalk_user_id": "user-keep",
        "name": "保留",
        "email": "keep@example.com",
        "mobile": "13800000000",
        "employee_number": "ET-001",
        "status": "active",
        "active": True,
    }
    payload.update(overrides)
    return DirectoryUserRecord(**payload)


def _meta(*, authoritative: bool = True, complete: bool = True, stale: bool = False, snapshot_id: str = "snap-1"):
    return DirectorySnapshotMeta(
        snapshot_id=snapshot_id,
        complete=complete,
        stale=stale,
        authoritative=authoritative,
    )


class FakeReader:
    def __init__(self, result: DirectorySnapshotRead | None = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def read_full_snapshot(self) -> DirectorySnapshotRead:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class InMemoryProjectionPort:
    def __init__(self, existing: dict[str, DirectoryUserRecord] | None = None) -> None:
        self.users = dict(existing or {})
        self.inactive: set[str] = set()
        self.calls: list[str] = []
        self._stash_users: dict[str, DirectoryUserRecord] | None = None
        self._stash_inactive: set[str] | None = None
        self.committed = False
        self.rolled_back = False
        self.fail_on_upsert: str | None = None
        self.fail_exc: BaseException | None = None

    def begin(self, snapshot: DirectorySnapshotMeta) -> None:
        self.calls.append("begin")
        self._stash_users = dict(self.users)
        self._stash_inactive = set(self.inactive)
        self.committed = False
        self.rolled_back = False
        self.snapshot = snapshot

    def upsert_user(self, user: DirectoryUserRecord) -> str:
        self.calls.append(f"upsert:{user.user_ref}")
        if self.fail_on_upsert == user.user_ref:
            raise self.fail_exc if self.fail_exc is not None else RuntimeError("投影失败")
        previous = self.users.get(user.user_ref)
        self.inactive.discard(user.user_ref)
        if previous is None:
            self.users[user.user_ref] = user
            return "created"
        if previous == user:
            return "unchanged"
        self.users[user.user_ref] = user
        return "updated"

    def deactivate_missing(self, seen_user_refs: frozenset[str]) -> int:
        self.calls.append("deactivate_missing")
        count = 0
        for ref in self.users:
            if ref not in seen_user_refs and ref not in self.inactive:
                self.inactive.add(ref)
                count += 1
        return count

    def commit(self) -> None:
        self.calls.append("commit")
        self.committed = True
        self._stash_users = None
        self._stash_inactive = None

    def rollback(self) -> None:
        self.calls.append("rollback")
        self.rolled_back = True
        self.committed = False
        if self._stash_users is not None:
            self.users = self._stash_users
            self.inactive = self._stash_inactive or set()
            self._stash_users = None
            self._stash_inactive = None


def _run(reader: FakeReader, port: InMemoryProjectionPort):
    return run_directory_sync(reader, port, actor_id="actor-1", clock=lambda: SYNC_AT)


def test_authoritative_snapshot_upserts_and_deactivates_missing() -> None:
    kept = _user()
    outdated = _user(user_ref="dt:v1:update", user_id="ak-update", dingtalk_user_id="user-update", name="旧名")
    departed = _user(user_ref="dt:v1:gone", user_id="ak-gone", dingtalk_user_id="user-gone", name="离职")
    incoming_kept = kept
    incoming_updated = _user(user_ref="dt:v1:update", user_id="ak-update", dingtalk_user_id="user-update", name="新名")
    incoming_new = _user(user_ref="dt:v1:new", user_id="ak-new", dingtalk_user_id="user-new", name="新人")
    incoming_unmapped = _user(
        user_ref="dt:v1:unmapped",
        user_id=None,
        dingtalk_user_id="user-unmapped",
        name="无登录",
    )
    port = InMemoryProjectionPort({kept.user_ref: kept, outdated.user_ref: outdated, departed.user_ref: departed})
    reader = FakeReader(
        DirectorySnapshotRead(
            users=(incoming_kept, incoming_updated, incoming_new, incoming_unmapped),
            snapshot=_meta(),
        )
    )

    result = _run(reader, port)

    assert result.status == "completed"
    assert result.at == SYNC_AT
    assert result.authoritative is True
    assert result.complete is True
    assert result.stale is False
    assert result.snapshot_id == "snap-1"
    assert result.upstream_total == 4
    assert (result.created, result.updated, result.unchanged, result.deactivated, result.unmapped) == (2, 1, 1, 1, 1)
    assert port.committed is True
    assert port.rolled_back is False
    assert port.inactive == {"dt:v1:gone"}
    assert port.users["dt:v1:update"].name == "新名"
    assert port.users["dt:v1:unmapped"].user_id is None
    assert "begin" in port.calls
    assert "deactivate_missing" in port.calls
    assert "commit" in port.calls
    assert "rollback" not in port.calls


def test_not_authoritative_snapshot_writes_nothing() -> None:
    existing = _user()
    port = InMemoryProjectionPort({existing.user_ref: existing})
    incoming = _user(user_ref="dt:v1:new", user_id=None, dingtalk_user_id="user-new", name="新人")
    reader = FakeReader(
        DirectorySnapshotRead(
            users=(existing, incoming),
            snapshot=_meta(authoritative=False, complete=False, stale=True, snapshot_id="stale-1"),
        )
    )

    result = _run(reader, port)

    assert result.status == "not_authoritative"
    assert result.authoritative is False
    assert result.complete is False
    assert result.stale is True
    assert result.snapshot_id == "stale-1"
    assert result.upstream_total == 2
    assert (result.created, result.updated, result.unchanged, result.deactivated) == (0, 0, 0, 0)
    assert result.unmapped == 1
    assert port.calls == []
    assert port.users == {existing.user_ref: existing}
    assert port.inactive == set()


@pytest.mark.parametrize(
    ("complete", "stale"),
    (
        (False, False),
        (True, True),
        (False, True),
    ),
)
def test_inconsistent_authoritative_metadata_writes_nothing(complete: bool, stale: bool) -> None:
    existing = _user()
    port = InMemoryProjectionPort({existing.user_ref: existing})
    incoming = _user(user_ref="dt:v1:new", user_id=None, dingtalk_user_id="user-new", name="新人")
    reader = FakeReader(
        DirectorySnapshotRead(
            users=(existing, incoming),
            snapshot=_meta(authoritative=True, complete=complete, stale=stale, snapshot_id="inconsistent-1"),
        )
    )

    result = _run(reader, port)

    assert result.status == "not_authoritative"
    assert result.authoritative is True
    assert result.complete is complete
    assert result.stale is stale
    assert result.snapshot_id == "inconsistent-1"
    assert (result.created, result.updated, result.unchanged, result.deactivated) == (0, 0, 0, 0)
    assert "元数据不一致" in result.summary
    assert port.calls == []
    assert port.users == {existing.user_ref: existing}
    assert port.inactive == set()


def test_snapshot_drift_writes_nothing() -> None:
    existing = _user()
    port = InMemoryProjectionPort({existing.user_ref: existing})
    reader = FakeReader(error=DirectorySnapshotDriftError("snapshot_changed"))

    result = _run(reader, port)

    assert result.status == "drift"
    assert result.created == 0
    assert result.deactivated == 0
    assert result.error_detail == "DirectorySnapshotDriftError: 目录快照在读取期间发生变化"
    assert "未写入" in result.summary
    assert port.calls == []
    assert port.users == {existing.user_ref: existing}


def test_projection_exception_rolls_back_and_marks_failed() -> None:
    existing = _user()
    incoming = _user(user_ref="dt:v1:boom", user_id="ak-boom", dingtalk_user_id="user-boom", name="爆炸")
    port = InMemoryProjectionPort({existing.user_ref: existing})
    port.fail_on_upsert = incoming.user_ref
    port.fail_exc = Exception(
        'duplicate key value violates unique constraint "users_email_key" '
        "DETAIL: Key (email)=(keep@example.com) already exists."
    )
    reader = FakeReader(DirectorySnapshotRead(users=(existing, incoming), snapshot=_meta()))

    result = _run(reader, port)

    assert result.status == "failed"
    assert result.error_detail == "Exception"
    assert "keep@example.com" not in (result.error_detail or "")
    assert "email" not in (result.error_detail or "").lower()
    assert result.authoritative is True
    assert result.snapshot_id == "snap-1"
    assert port.rolled_back is True
    assert port.committed is False
    assert port.users == {existing.user_ref: existing}
    assert port.calls[0] == "begin"
    assert port.calls[-1] == "rollback"
    assert "commit" not in port.calls


def test_reader_exception_rolls_back_and_marks_failed() -> None:
    port = InMemoryProjectionPort()
    reader = FakeReader(error=RuntimeError("目录不可用"))

    result = _run(reader, port)

    assert result.status == "failed"
    assert result.error_detail == "RuntimeError"
    assert "目录不可用" not in (result.error_detail or "")
    assert port.calls == ["rollback"]
    assert port.rolled_back is True
    assert port.committed is False


def test_known_directory_unavailable_error_detail_omits_raw_text() -> None:
    port = InMemoryProjectionPort()
    reader = FakeReader(error=DirectoryUnavailableError("GET users leaked keep@example.com"))

    result = _run(reader, port)

    assert result.status == "failed"
    assert result.error_detail == "DirectoryUnavailableError: 目录暂不可用"
    assert "keep@example.com" not in (result.error_detail or "")
    assert port.calls == ["rollback"]


def _admin_headers(client):
    import os

    from blank_app.database import SessionLocal
    from blank_app.models import Account
    from enterprise_platform.rate_limit import reset_rate_limits

    reset_rate_limits()
    with SessionLocal() as db:
        admin = db.query(Account).filter(Account.username == os.environ["BLANK_ADMIN_USERNAME"]).one()
        admin.must_change_password = False
        db.commit()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": os.environ["BLANK_ADMIN_PASSWORD"]},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


@pytest.mark.usefixtures("blank_admin_seeded")
def test_blank_directory_routes_persist_settings_and_return_not_configured() -> None:
    from fastapi.testclient import TestClient

    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import PlatformSetting
    from enterprise_platform.secrets import decrypt_secret

    with TestClient(app) as client:
        headers = _admin_headers(client)
        listed = client.get("/api/v1/identity-integration/directory", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["enabled"] is False
        assert listed.json()["hasCredential"] is False
        assert listed.json()["lastSync"] is None

        saved = client.put(
            "/api/v1/identity-integration/directory",
            headers=headers,
            json={
                "enabled": True,
                "baseUrl": "https://easyauth.example.com",
                "appKey": "enterprise-blank",
                "credential": "eat_directory_test_token",
                "authMode": "static_app_token",
                "syncIntervalMinutes": 15,
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["enabled"] is True
        assert body["baseUrl"] == "https://easyauth.example.com"
        assert body["appKey"] == "enterprise-blank"
        assert body["hasCredential"] is True
        assert body["authMode"] == "static_app_token"
        assert body["syncIntervalMinutes"] == 15
        assert "credential" not in body

        probed = client.post("/api/v1/identity-integration/directory/test", headers=headers)
        assert probed.status_code == 200
        assert probed.json()["ok"] is False
        assert probed.json()["errorKind"] == "not_configured"

        synced = client.post("/api/v1/identity-integration/directory/sync", headers=headers)
        assert synced.status_code == 200
        assert synced.json()["status"] == "not_configured"
        assert synced.json()["created"] == 0
        assert synced.json()["deactivated"] == 0

        after_sync = client.get("/api/v1/identity-integration/directory", headers=headers)
        assert after_sync.json()["lastSync"]["status"] == "not_configured"

        removed = client.post("/api/v1/identity-integration/user-sync", headers=headers)
        assert removed.status_code == 404

        assert client.get("/api/v1/identity-integration/directory").status_code == 401

    with SessionLocal() as db:
        stored = db.get(PlatformSetting, "directory").value
        assert stored["enabled"] is True
        assert stored["has_credential"] is True
        assert decrypt_secret(stored["credential"]) == "eat_directory_test_token"
        assert stored["credential"].startswith("enc:v1:")


@pytest.mark.usefixtures("blank_admin_seeded")
def test_blank_directory_routes_redact_and_forbid_view_only_identity_user() -> None:
    import uuid

    from fastapi.testclient import TestClient

    from blank_app.adapters import pwd_context
    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import Account

    view_username = f"dir-view-{uuid.uuid4().hex[:8]}"
    view_password = "View-only-directory-password!"
    with SessionLocal() as db:
        db.add(
            Account(
                username=view_username,
                password_hash=pwd_context.hash(view_password),
                active=True,
                is_admin=False,
                must_change_password=False,
                local_permissions=[{"code": "identity.integration.view", "scope": "ALL"}],
            )
        )
        db.commit()

    with TestClient(app) as client:
        headers = _admin_headers(client)
        saved = client.put(
            "/api/v1/identity-integration/directory",
            headers=headers,
            json={
                "enabled": True,
                "baseUrl": "https://easyauth.example.com",
                "appKey": "enterprise-blank",
                "credential": "eat_directory_test_token",
                "authMode": "static_app_token",
                "syncIntervalMinutes": 15,
            },
        )
        assert saved.status_code == 200, saved.text

        view_login = client.post(
            "/api/v1/auth/login",
            json={"username": view_username, "password": view_password},
        )
        assert view_login.status_code == 200, view_login.text
        view_headers = {"Authorization": f"Bearer {view_login.json()['accessToken']}"}

        listed = client.get("/api/v1/identity-integration/directory", headers=view_headers)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert set(body) == {"enabled", "configured", "hasCredential"}
        assert body == {"enabled": True, "configured": True, "hasCredential": True}

        denied_put = client.put(
            "/api/v1/identity-integration/directory",
            headers=view_headers,
            json={
                "enabled": False,
                "baseUrl": "https://easyauth.example.com",
                "appKey": "enterprise-blank",
                "credential": "must-not-write",
                "authMode": "static_app_token",
                "syncIntervalMinutes": 30,
            },
        )
        assert denied_put.status_code == 403
        assert client.post("/api/v1/identity-integration/directory/test", headers=view_headers).status_code == 403
        assert client.post("/api/v1/identity-integration/directory/sync", headers=view_headers).status_code == 403
        still = client.get("/api/v1/identity-integration/directory", headers=headers)
        assert still.json()["enabled"] is True
        assert still.json()["hasCredential"] is True
        assert still.json()["baseUrl"] == "https://easyauth.example.com"
