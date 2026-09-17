"""认证热路径:后台刷新 backoff 与 worker 失败。"""

from __future__ import annotations

import logging
import uuid

import blank_app.authz_hotpath as hotpath
from blank_app.authz_snapshot import _in_failure_backoff


def test_worker_unexpected_exception_logs_and_backs_off(monkeypatch, caplog) -> None:
    monkeypatch.setattr(hotpath, "_still_needs_refresh", lambda _account_id: True)

    def boom(*_args, **_kwargs):
        raise RuntimeError("db blip")

    monkeypatch.setattr("blank_app.authz_snapshot._refresh_under_lock", boom)
    caplog.set_level(logging.ERROR)
    key = f"exc-{uuid.uuid4().hex}:user"
    hotpath.schedule_background_refresh(uuid.uuid4(), key)
    hotpath.wait_background_refreshes()
    assert _in_failure_backoff(key)
    assert any("authz background refresh failed" in rec.message for rec in caplog.records)


def test_schedule_skips_when_in_failure_backoff(monkeypatch) -> None:
    monkeypatch.setattr("blank_app.authz_snapshot._in_failure_backoff", lambda _key: True)
    scheduled = {"n": 0}
    original = hotpath._refresher.schedule

    def counted(key, fn):
        scheduled["n"] += 1
        return original(key, fn)

    monkeypatch.setattr(hotpath._refresher, "schedule", counted)
    hotpath.schedule_background_refresh(uuid.uuid4(), "backoff:user")
    assert scheduled["n"] == 0
