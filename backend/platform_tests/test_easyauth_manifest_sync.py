"""EasyAuth manifest 推送:版本、跳过、409/422 与失败是否落库。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from enterprise_platform.easyauth import ManifestSyncTarget, manifest_content_hash, sync_manifest

BASE = "https://easyauth.example.test"
APP_KEY = "enterprise-blank"
TOKEN = "eat_manifest_test"


class _MemoryStore:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = None if state is None else dict(state)
        self.events: list[str] = []

    def load_state(self) -> dict[str, Any] | None:
        self.events.append("load")
        return None if self.state is None else dict(self.state)

    def save_state(self, state: dict[str, Any]) -> None:
        self.events.append("save")
        self.state = dict(state)


def _target(**overrides: Any) -> ManifestSyncTarget:
    values: dict[str, Any] = {
        "base_url": BASE,
        "app_key": APP_KEY,
        "token": TOKEN,
        "public_base_url": None,
    }
    values.update(overrides)
    return ManifestSyncTarget(
        base_url=str(values["base_url"]),
        app_key=str(values["app_key"]),
        token=str(values["token"]),
        public_base_url=values["public_base_url"],
    )


def _manifest(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "app": {"app_key": APP_KEY, "name": "Demo"},
        "permissions": [{"key": "demo.read"}],
    }
    payload.update(overrides)
    return payload


def _accepted() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "app_key": APP_KEY,
            "already_up_to_date": False,
            "template_version": 3,
            "catalog_version": 4,
        },
    )


def _error(status: int, code: str = "ERROR") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "nope", "details": {}}})


def _conflict(latest: int, incoming: int = 1) -> httpx.Response:
    # 与 manifest_import.ManifestVersionConflictError 的报文一致,括号前有空格。
    message = (
        f"下游 manifest schema_version({incoming}) 未超过已导入版本 ({latest}) 且内容不一致, 请在下游递增版本后重试。"
    )
    return httpx.Response(409, json={"error": {"code": "CONFLICT", "message": message, "details": {}}})


def _play(responses: list[httpx.Response]) -> tuple[list[dict[str, Any]], list[httpx.Request], httpx.MockTransport]:
    bodies: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(json.loads(request.content.decode()))
        return responses[len(bodies) - 1]

    return bodies, requests, httpx.MockTransport(handler)


def test_content_hash_ignores_schema_version_and_key_order() -> None:
    left = {"schema_version": 1, "b": 1, "a": {"z": 1, "y": 2}}
    right = {"a": {"y": 2, "z": 1}, "b": 1, "schema_version": 9}
    assert manifest_content_hash(left) == manifest_content_hash(right)
    assert manifest_content_hash({"a": 1}) != manifest_content_hash({"a": 2})


def test_first_push_posts_manifest_and_records_ok() -> None:
    store = _MemoryStore()
    manifest = _manifest(schema_version=4)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert store.events == ["load"]
        captured["authorization"] = request.headers["Authorization"]
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return _accepted()

    result = sync_manifest(target=_target(), manifest=manifest, store=store, transport=httpx.MockTransport(handler))
    assert manifest["schema_version"] == 4
    assert captured["authorization"] == f"Bearer {TOKEN}"
    assert captured["path"] == f"/api/v1/apps/{APP_KEY}/manifest-sync"
    assert set(captured["body"]) == {"manifest"}
    assert captured["body"]["manifest"]["schema_version"] == 4
    assert result.pushed is True
    assert result.skipped is False
    assert result.status == "ok"
    assert result.already_up_to_date is False
    assert result.template_version == 3
    assert result.catalog_version == 4
    assert store.events == ["load", "save"]
    assert store.state is not None
    assert store.state["status"] == "ok"
    assert store.state["schema_version"] == 4
    assert store.state["content_hash"] == manifest_content_hash(manifest)
    assert store.state["base_url"] == BASE
    assert store.state["app_key"] == APP_KEY
    assert store.state["template_version"] == 3
    assert store.state["catalog_version"] == 4
    assert store.state["synced_at"]


def test_unchanged_manifest_skips_http() -> None:
    store = _MemoryStore()
    manifest = _manifest()
    bodies, _, transport = _play([_accepted(), _accepted()])
    first = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    second = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert first.pushed is True
    assert second.skipped is True
    assert second.pushed is False
    assert len(bodies) == 1
    assert store.events == ["load", "save", "load"]


def test_trailing_slash_on_same_destination_does_not_republish() -> None:
    store = _MemoryStore()
    manifest = _manifest()
    bodies, _, transport = _play([_accepted(), _accepted()])
    sync_manifest(target=_target(base_url=f"{BASE}/"), manifest=manifest, store=store, transport=transport)
    second = sync_manifest(target=_target(base_url=BASE), manifest=manifest, store=store, transport=transport)
    assert second.skipped is True
    assert len(bodies) == 1
    assert store.state is not None
    assert store.state["base_url"] == BASE


def test_content_change_bumps_schema_version() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_accepted(), _accepted()])
    sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    changed = _manifest(permissions=[{"key": "demo.write"}])
    result = sync_manifest(target=_target(), manifest=changed, store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 2]
    assert result.schema_version == 2
    assert result.pushed is True


def test_local_schema_version_floor_beats_stored_plus_one() -> None:
    manifest = _manifest(schema_version=5, permissions=[{"key": "demo.write"}])
    store = _MemoryStore(
        {
            "content_hash": "stale",
            "schema_version": 3,
            "status": "ok",
            "base_url": BASE,
            "app_key": APP_KEY,
        }
    )
    bodies, _, transport = _play([_accepted()])
    result = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert bodies[0]["manifest"]["schema_version"] == 5
    assert result.schema_version == 5


def test_unchanged_content_reuses_stored_version_when_forced() -> None:
    manifest = _manifest(schema_version=1)
    store = _MemoryStore(
        {
            "content_hash": manifest_content_hash(manifest),
            "schema_version": 6,
            "status": "ok",
            "base_url": BASE,
            "app_key": APP_KEY,
        }
    )
    bodies, _, transport = _play([_accepted()])
    result = sync_manifest(target=_target(), manifest=manifest, store=store, force=True, transport=transport)
    assert bodies[0]["manifest"]["schema_version"] == 6
    assert result.schema_version == 6
    assert manifest["schema_version"] == 1


def test_409_retries_once_with_downstream_version() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_conflict(9), _accepted()])
    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 10]
    assert result.pushed is True
    assert result.schema_version == 10
    assert store.state is not None
    assert store.state["schema_version"] == 10
    assert store.state["status"] == "ok"


def test_409_without_version_retries_only_once() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_error(409), _error(409)])
    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 2]
    assert result.pushed is False
    assert result.status == "failed"
    assert store.state is not None
    assert store.state["status"] == "failed"
    assert store.state["schema_version"] == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_retryable_http_is_not_persisted(status: int) -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_error(status), _error(status)])
    first = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    second = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert first.pushed is False
    assert first.status is None
    assert store.state is None
    assert second.skipped is False
    assert len(bodies) == 2


def test_network_error_is_not_persisted() -> None:
    store = _MemoryStore()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=httpx.MockTransport(handler))
    assert result.pushed is False
    assert result.status is None
    assert store.state is None
    assert store.events == ["load"]


def test_422_retries_without_base_url() -> None:
    store = _MemoryStore()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "base_url" in body:
            return _error(422)
        return _accepted()

    result = sync_manifest(
        target=_target(public_base_url="https://blank.example.test/"),
        manifest=_manifest(),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    assert result.pushed is True
    assert bodies[0]["base_url"] == "https://blank.example.test"
    assert "base_url" not in bodies[1]
    assert bodies[0]["manifest"] == bodies[1]["manifest"]
    assert store.state is not None
    assert store.state["status"] == "ok"


def test_422_without_base_url_is_failed() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_error(422)])
    result = sync_manifest(
        target=_target(public_base_url="http://blank.example.test"),
        manifest=_manifest(),
        store=store,
        transport=transport,
    )
    assert len(bodies) == 1
    assert "base_url" not in bodies[0]
    assert result.status == "failed"
    assert store.state is not None
    assert store.state["status"] == "failed"


def test_non_https_public_base_url_is_omitted() -> None:
    bodies, requests, transport = _play([_accepted()])
    sync_manifest(
        target=_target(public_base_url="http://blank.example.test"),
        manifest=_manifest(),
        store=_MemoryStore(),
        transport=transport,
    )
    assert "base_url" not in bodies[0]
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_destination_change_re_pushes() -> None:
    store = _MemoryStore()
    manifest = _manifest()
    bodies, _, transport = _play([_accepted(), _accepted()])
    sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    result = sync_manifest(
        target=_target(base_url="https://other.example.test"),
        manifest=manifest,
        store=store,
        transport=transport,
    )
    assert result.pushed is True
    assert len(bodies) == 2
    assert store.state is not None
    assert store.state["base_url"] == "https://other.example.test"
    assert store.state["status"] == "ok"


def test_force_bypasses_skip() -> None:
    store = _MemoryStore()
    manifest = _manifest()
    bodies, _, transport = _play([_accepted(), _accepted()])
    first = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    second = sync_manifest(target=_target(), manifest=manifest, store=store, force=True, transport=transport)
    assert first.pushed is True
    assert second.pushed is True
    assert second.skipped is False
    assert len(bodies) == 2
    assert bodies[1]["manifest"]["schema_version"] == 1


def test_other_4xx_is_retried_and_later_success_overwrites() -> None:
    store = _MemoryStore()
    manifest = _manifest()
    bodies, _, transport = _play([_error(400), _accepted()])
    first = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert first.status == "failed"
    assert store.state is not None
    failed = dict(store.state)
    assert failed["status"] == "failed"
    assert failed["content_hash"] == manifest_content_hash(manifest)
    assert "template_version" not in failed
    second = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert second.pushed is True
    assert len(bodies) == 2
    assert store.state["status"] == "ok"
    assert store.state["template_version"] == 3
    assert store.state["catalog_version"] == 4


def test_invalid_success_payload_is_not_persisted() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([httpx.Response(200, json={"already_up_to_date": True}), _accepted()])
    first = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert first.pushed is False
    assert store.state is None
    second = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert second.pushed is True
    assert len(bodies) == 2
