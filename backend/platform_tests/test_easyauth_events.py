"""EasyAuth 入站事件端点:签名请求打到假 port,不访问上游。"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_platform.assembly import PlatformPorts, PlatformRouteGroups, create_platform_router
from enterprise_platform.easyauth.webhook import CATALOG_CHANGED_EVENT, GRANT_CHANGED_EVENT, WEBHOOK_TEST_EVENT
from enterprise_platform.schemas import CurrentUser, EasyAuthStatus

SECRET = "whsec_endpoint"


class _FakeIntegrations:
    def __init__(self, app_key: str = "enterprise-blank") -> None:
        self.app_key = app_key

    def get_easyauth_webhook_secret(self) -> str:
        return SECRET

    def get_easyauth_status(self) -> EasyAuthStatus:
        return EasyAuthStatus(app_key=self.app_key)


class _FakeAuthorization:
    def __init__(self) -> None:
        self.refreshed: list[tuple[str, str]] = []
        self.invalidated: list[str] = []

    def refresh_snapshot_for_external_user(self, external_user_id: str, expected_snapshot_version: str) -> None:
        self.refreshed.append((external_user_id, expected_snapshot_version))

    def invalidate_app_snapshots(self, app_key: str, catalog_version: int) -> None:
        self.invalidated.append((app_key, catalog_version))


def _signed(body: bytes, *, event: str, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(datetime.now(UTC).timestamp()) if timestamp is None else timestamp)
    signature = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "X-EasyAuth-Event": event,
        "X-EasyAuth-Delivery": "dlv-1",
        "X-EasyAuth-Timestamp": ts,
        "X-EasyAuth-Signature": signature,
    }


def _payload(event_type: str, **fields: Any) -> bytes:
    return json.dumps({"event_type": event_type, **fields}, sort_keys=True).encode()


def _client() -> tuple[TestClient, _FakeAuthorization]:
    authz = _FakeAuthorization()
    app = FastAPI()
    app.include_router(
        create_platform_router(
            PlatformPorts(
                account=None,
                footer=None,
                notifications=None,
                integrations=_FakeIntegrations(),
                directory=None,
                upstream_health=None,
                require_permission=lambda _code: None,
                authorization=authz,
            ),
            include_authz_integration=False,
            route_groups=PlatformRouteGroups(
                auth=False,
                me=False,
                password=False,
                totp=False,
                passkeys=False,
                footer=False,
                notifications=False,
                identity=False,
                easyauth=True,
                upstream=False,
            ),
            current_user_dependency=lambda: CurrentUser(id="actor", name="Actor"),
            permission_dependency_factory=lambda _code: lambda: None,
        ),
        prefix="/api/v1",
    )
    return TestClient(app), authz


def test_webhook_test_returns_ok() -> None:
    client, authz = _client()
    body = _payload(WEBHOOK_TEST_EVENT)
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=WEBHOOK_TEST_EVENT))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert authz.refreshed == []
    assert authz.invalidated == []


def test_grant_changed_refreshes_external_user() -> None:
    client, authz = _client()
    body = _payload(
        GRANT_CHANGED_EVENT,
        app_key="enterprise-blank",
        user_id="ak-user-1",
        grant_version=4,
        catalog_version=2,
        snapshot_version="4.2",
        changed_at=datetime.now(UTC).isoformat(),
    )
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=GRANT_CHANGED_EVENT))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert authz.refreshed == [("ak-user-1", "4.2")]


def test_catalog_changed_invalidates_app() -> None:
    client, authz = _client()
    body = _payload(CATALOG_CHANGED_EVENT, app_key="enterprise-blank", catalog_version=9)
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=CATALOG_CHANGED_EVENT))
    assert response.status_code == 200, response.text
    assert authz.invalidated == [("enterprise-blank", 9)]


def test_unknown_event_is_unsupported() -> None:
    client, _authz = _client()
    body = _payload("approval.completed")
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event="approval.completed"))
    assert response.status_code == 422
    assert response.json() == {"error": "unsupported_event"}


def test_wrong_app_key_is_unprocessable() -> None:
    client, authz = _client()
    body = _payload(
        GRANT_CHANGED_EVENT,
        app_key="other-app",
        user_id="ak-user-1",
        snapshot_version="4.2",
    )
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=GRANT_CHANGED_EVENT))
    assert response.status_code == 422
    assert response.json() == {"error": "app_key_mismatch"}
    assert authz.refreshed == []
    assert authz.invalidated == []


def test_missing_app_key_is_unprocessable() -> None:
    client, authz = _client()
    body = _payload(GRANT_CHANGED_EVENT, user_id="ak-user-1", snapshot_version="4.2")
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=GRANT_CHANGED_EVENT))
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_payload"}
    assert authz.refreshed == []


def test_malformed_timestamp_is_unauthorized() -> None:
    client, authz = _client()
    body = _payload(WEBHOOK_TEST_EVENT)
    headers = _signed(body, event=WEBHOOK_TEST_EVENT)
    headers["X-EasyAuth-Timestamp"] = "9" * 32
    response = client.post("/api/v1/easyauth/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}
    assert authz.refreshed == []


def test_non_ascii_signature_is_unauthorized() -> None:
    client, authz = _client()
    body = _payload(WEBHOOK_TEST_EVENT)
    headers = _signed(body, event=WEBHOOK_TEST_EVENT)
    headers["X-EasyAuth-Signature"] = b"\xdf" * 64
    response = client.post("/api/v1/easyauth/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}
    assert authz.refreshed == []


def test_dispatch_runs_in_threadpool(monkeypatch) -> None:
    import enterprise_platform.assembly.easyauth_event_routes as routes

    seen: list[object] = []

    async def fake_threadpool(func, *args, **kwargs):
        seen.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(routes, "run_in_threadpool", fake_threadpool)
    client, authz = _client()
    body = _payload(
        GRANT_CHANGED_EVENT,
        app_key="enterprise-blank",
        user_id="ak-user-1",
        snapshot_version="4.2",
    )
    response = client.post("/api/v1/easyauth/events", content=body, headers=_signed(body, event=GRANT_CHANGED_EVENT))
    assert response.status_code == 200, response.text
    assert seen
    assert authz.refreshed == [("ak-user-1", "4.2")]


def test_bad_signature_is_unauthorized() -> None:
    client, _authz = _client()
    body = _payload(WEBHOOK_TEST_EVENT)
    headers = _signed(body, event=WEBHOOK_TEST_EVENT)
    headers["X-EasyAuth-Signature"] = "0" * 64
    response = client.post("/api/v1/easyauth/events", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}
