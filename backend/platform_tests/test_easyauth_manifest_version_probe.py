"""409 先探测已导入版本;失败不抬地板;422 不重复打同一目的地。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from enterprise_platform.easyauth import manifest_content_hash, sync_manifest
from platform_tests.test_easyauth_manifest_sync import (
    APP_KEY,
    BASE,
    _accepted,
    _conflict,
    _error,
    _manifest,
    _MemoryStore,
    _play,
    _target,
)


def _up_to_date(version: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "app_key": APP_KEY,
            "already_up_to_date": True,
            "template_version": version,
            "catalog_version": version,
        },
    )


def _accepted_state(manifest: dict[str, Any], version: int = 4) -> dict[str, Any]:
    return {
        "content_hash": manifest_content_hash(manifest),
        "schema_version": version,
        "status": "ok",
        "base_url": BASE,
        "app_key": APP_KEY,
        "template_version": version,
        "catalog_version": 8,
    }


def test_409_probe_stops_when_content_is_already_imported() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_conflict(9), _up_to_date(9)])
    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 9]
    assert result.pushed is True
    assert result.already_up_to_date is True
    assert result.schema_version == 9
    assert result.template_version == 9
    assert store.state is not None
    assert store.state["schema_version"] == 9
    assert store.state["template_version"] == 9
    assert store.state["status"] == "ok"


def test_409_probe_conflict_pushes_next_version_once() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_conflict(9), _conflict(9, incoming=9), _accepted(), _conflict(11)])
    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 9, 10]
    assert result.pushed is True
    assert result.schema_version == 10
    assert store.state is not None
    assert store.state["schema_version"] == 10
    assert store.state["status"] == "ok"


def test_bump_conflict_does_not_keep_climbing() -> None:
    store = _MemoryStore()
    bodies, _, transport = _play([_conflict(9), _conflict(9), _conflict(10), _conflict(11)])
    result = sync_manifest(target=_target(), manifest=_manifest(), store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 9, 10]
    assert result.status == "failed"
    assert store.state is None


def test_opening_version_equal_to_latest_bumps_without_a_second_probe() -> None:
    manifest = _manifest(schema_version=9)
    store = _MemoryStore()
    bodies, _, transport = _play([_conflict(9, incoming=9), _accepted()])
    result = sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert [body["manifest"]["schema_version"] for body in bodies] == [9, 10]
    assert result.schema_version == 10


def test_rejection_leaves_last_accepted_row() -> None:
    original = _manifest()
    changed = _manifest(permissions=[{"key": "demo.write"}])
    store = _MemoryStore(_accepted_state(original))
    bodies, _, transport = _play([_error(400)])
    result = sync_manifest(target=_target(), manifest=changed, store=store, transport=transport)
    assert result.status == "failed"
    assert bodies[0]["manifest"]["schema_version"] == 5
    assert store.state == _accepted_state(original)


def test_failed_schema_version_is_not_the_next_floor() -> None:
    manifest = _manifest()
    store = _MemoryStore(
        {
            "content_hash": manifest_content_hash(manifest),
            "schema_version": 6,
            "status": "failed",
            "base_url": BASE,
            "app_key": APP_KEY,
        }
    )
    bodies, _, transport = _play([_accepted()])
    sync_manifest(target=_target(), manifest=manifest, store=store, transport=transport)
    assert bodies[0]["manifest"]["schema_version"] == 1


def test_422_after_success_keeps_version_and_skips_the_same_hash() -> None:
    original = _manifest()
    changed = _manifest(permissions=[{"key": "demo.write"}])
    store = _MemoryStore()
    bodies, _, transport = _play([_accepted(), _error(422), _accepted()])
    sync_manifest(target=_target(), manifest=original, store=store, transport=transport)
    rejected = sync_manifest(target=_target(), manifest=changed, store=store, transport=transport)
    assert rejected.status == "failed"
    assert store.state is not None
    assert store.state["status"] == "ok"
    assert store.state["schema_version"] == 1
    assert store.state["template_version"] == 3
    assert store.state["rejected_hash"] == manifest_content_hash(changed)
    assert store.state["http_status"] == 422
    again = sync_manifest(target=_target(), manifest=changed, store=store, transport=transport)
    assert again.skipped is True
    assert len(bodies) == 2
    forced = sync_manifest(target=_target(), manifest=changed, store=store, force=True, transport=transport)
    assert forced.pushed is True
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 2, 2]


def test_one_base_url_strip_across_the_version_probe() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if len(bodies) == 1:
            return _error(422)
        version = body["manifest"]["schema_version"]
        if version < 4:
            return _conflict(4)
        if version == 4:
            return _up_to_date(4)
        raise AssertionError(version)

    result = sync_manifest(
        target=_target(public_base_url="https://blank.example.test"),
        manifest=_manifest(),
        store=_MemoryStore(),
        transport=httpx.MockTransport(handler),
    )
    assert result.already_up_to_date is True
    assert [body["manifest"]["schema_version"] for body in bodies] == [1, 1, 4]
    assert bodies[0]["base_url"] == "https://blank.example.test"
    assert all("base_url" not in body for body in bodies[1:])
