"""快照新鲜度分类与 BackgroundRefresher 去重 / 回收。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_platform.authz.snapshot_freshness import (
    NEAR_EXPIRY_RATIO,
    STALE_GRACE,
    BackgroundRefresher,
    SnapshotFreshness,
    classify_snapshot,
    invalidated_expires_at,
)

_FETCHED = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return _FETCHED + timedelta(seconds=seconds)


def test_fresh_when_remaining_at_least_near_expiry_ratio() -> None:
    expires = _at(100)
    # remaining == 40% 窗口用 < 判定,边界仍是 FRESH。
    assert classify_snapshot(_FETCHED, expires, _at(60)) is SnapshotFreshness.FRESH
    assert NEAR_EXPIRY_RATIO == 0.40


def test_near_expiry_when_remaining_below_ratio() -> None:
    expires = _at(100)
    assert classify_snapshot(_FETCHED, expires, _at(61)) is SnapshotFreshness.NEAR_EXPIRY
    assert classify_snapshot(_FETCHED, expires, _at(99)) is SnapshotFreshness.NEAR_EXPIRY


def test_stale_grace_while_within_window_after_expiry() -> None:
    expires = _at(100)
    assert classify_snapshot(_FETCHED, expires, expires) is SnapshotFreshness.STALE_GRACE
    assert classify_snapshot(_FETCHED, expires, expires + STALE_GRACE - timedelta(seconds=1)) is (
        SnapshotFreshness.STALE_GRACE
    )


def test_expired_after_grace_or_when_invalidated() -> None:
    expires = _at(100)
    assert classify_snapshot(_FETCHED, expires, expires + STALE_GRACE) is SnapshotFreshness.EXPIRED
    fetched = _at(50)
    invalid = invalidated_expires_at(fetched)
    assert invalid == fetched
    assert classify_snapshot(fetched, invalid, fetched + timedelta(seconds=1)) is SnapshotFreshness.EXPIRED
    assert classify_snapshot(fetched, invalid, fetched) is SnapshotFreshness.EXPIRED


def test_schedule_dedups_inflight_and_wait_clears() -> None:
    refresher = BackgroundRefresher(max_workers=1, name="authz-refresh-test")
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def blocked() -> None:
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=5)

    try:
        assert refresher.schedule("app:user", blocked) is True
        assert entered.wait(timeout=5)
        assert refresher.schedule("app:user", blocked) is False
        release.set()
        refresher.wait()
        assert calls["n"] == 1
        assert refresher.schedule("app:user", lambda: None) is True
        refresher.wait()
    finally:
        refresher.shutdown()


def test_schedule_returns_false_after_shutdown() -> None:
    refresher = BackgroundRefresher()
    refresher.shutdown()
    assert refresher.schedule("app:user", lambda: None) is False


def test_submit_failure_clears_inflight() -> None:
    refresher = BackgroundRefresher()
    original = refresher._executor

    class Boom:
        def submit(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("executor closed")

    refresher._executor = lambda: Boom()  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="executor closed"):
            refresher.schedule("submit-fail:user", lambda: None)
    finally:
        refresher._executor = original
    assert refresher.schedule("submit-fail:user", lambda: None) is True
    refresher.wait()
    refresher.shutdown()


def test_cancelled_pending_future_clears_inflight(monkeypatch) -> None:
    occupied = threading.Event()
    hold = threading.Event()

    def occupy() -> None:
        occupied.set()
        hold.wait(timeout=5)

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="authz-cancel-test")
    refresher = BackgroundRefresher(max_workers=1)
    monkeypatch.setattr(refresher, "_executor", lambda: pool)
    pool.submit(occupy)
    assert occupied.wait(timeout=5)
    key = "cancel:user"
    try:
        assert refresher.schedule(key, lambda: time.sleep(30)) is True
        pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + 2
        while key in refresher._inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        assert key not in refresher._inflight
        submitted = {"n": 0}

        class Immediate:
            def submit(self, fn, *args, **kwargs):
                del fn, kwargs
                submitted["n"] += 1
                future: Future = Future()
                future.set_result(None)
                return future

        monkeypatch.setattr(refresher, "_executor", lambda: Immediate())
        assert refresher.schedule(key, lambda: None) is True
        assert submitted["n"] == 1
    finally:
        hold.set()
        refresher.shutdown()


def test_worker_unexpected_exception_logs(caplog) -> None:
    refresher = BackgroundRefresher()
    caplog.set_level(logging.ERROR)

    def boom() -> None:
        raise RuntimeError("db blip")

    assert refresher.schedule("exc:user", boom) is True
    refresher.wait()
    assert any("authz background refresh failed" in rec.message for rec in caplog.records)
    assert refresher.schedule("exc:user", lambda: None) is True
    refresher.wait()
    refresher.shutdown()
